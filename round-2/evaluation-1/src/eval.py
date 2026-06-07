#!/usr/bin/env python3
"""Rigorous Statistical Re-Evaluation of Dual-Signal SBFL with Perturbation Calibration.

Runs all 5 methods on 150 ProofWriter-NL depth-3 examples, computes bootstrap CIs,
perturbation calibration rho, KB completeness, atomic fact metrics, and generates 7 figures.
"""

import gc
import json
import math
import os
import random
import re
import resource
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from loguru import logger
from scipy import stats

# ── Logging ───────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent
(WORKSPACE / "logs").mkdir(exist_ok=True)

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add(str(WORKSPACE / "logs" / "eval.log"), rotation="30 MB", level="DEBUG")

# ── Hardware / memory limits ──────────────────────────────────────────────────
def _container_ram_gb() -> float:
    for p in ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
        try:
            v = Path(p).read_text().strip()
            if v != "max" and int(v) < 1_000_000_000_000:
                return int(v) / 1e9
        except (FileNotFoundError, ValueError):
            pass
    return 16.0

TOTAL_RAM_GB = _container_ram_gb()
RAM_BUDGET = int(TOTAL_RAM_GB * 0.65 * 1e9)
resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET * 3, RAM_BUDGET * 3))
logger.info(f"Container RAM: {TOTAL_RAM_GB:.1f}GB, budget: {RAM_BUDGET/1e9:.1f}GB")

# ── Configuration ─────────────────────────────────────────────────────────────
MODEL = "meta-llama/llama-3.1-8b-instruct"
MAX_BUDGET_USD = 9.0
N_EXAMPLES = 150
N_ORACLE_QUERIES = 5
N_REPAIR_ROUNDS = 1
K_REPAIR_TARGETS = 3
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
N_BOOTSTRAP = 10000
CALIB_N_DOCS = 10
CALIB_N_CORRUPTIONS = 5

COST_TRACKER: dict[str, float] = {"total": 0.0, "calls": 0}
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "meta-llama/llama-3.1-8b-instruct": (0.055, 0.055),
}


class BudgetExceededError(RuntimeError):
    pass


# ── LLM helper ────────────────────────────────────────────────────────────────
def llm_call(
    messages: list[dict],
    model: str = MODEL,
    max_tokens: int = 512,
    temperature: float = 0.0,
    retries: int = 3,
) -> tuple[str, float]:
    if COST_TRACKER["total"] >= MAX_BUDGET_USD:
        raise BudgetExceededError(f"Budget ${MAX_BUDGET_USD} exceeded")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=headers, json=payload, timeout=120,
            )
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage", {})
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            in_price, out_price = MODEL_PRICES.get(model, (0.1, 0.1))
            cost = (in_tok * in_price + out_tok * out_price) / 1_000_000
            COST_TRACKER["total"] += cost
            COST_TRACKER["calls"] += 1
            logger.debug(
                f"LLM #{COST_TRACKER['calls']} in={in_tok} out={out_tok} "
                f"cost=${cost:.5f} total=${COST_TRACKER['total']:.3f}"
            )
            return text, cost
        except BudgetExceededError:
            raise
        except Exception as e:
            last_err = e
            logger.warning(f"LLM attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    logger.error(f"All LLM retries failed: {last_err}")
    return "", 0.0


# ── Prolog infrastructure ─────────────────────────────────────────────────────
META_INTERPRETER_PROLOG = """\
:- dynamic coverage_log/3.
:- dynamic failed_subgoal/1.

reset_coverage :-
    retractall(coverage_log(_, _, _)),
    retractall(failed_subgoal(_)).

solve_safe(Goal, D) :-
    catch(
        call_with_depth_limit(solve(Goal, D), 50, _),
        _,
        fail
    ).

solve(true, _) :- !.
solve(fail, _) :- !, fail.
solve((A, B), D) :- !, (D > 0 -> D1 is D-1 ; D1 = 0), solve(A, D1), solve(B, D1).
solve((A ; B), D) :- !, (solve(A, D) ; solve(B, D)).
solve(\\+(A), D) :- !, (solve(A, D) -> fail ; true).
solve(Goal, D) :-
    D > 0,
    functor(Goal, Name, Arity),
    (catch(clause(Goal, Body), _, fail) ->
        D1 is D - 1,
        (solve(Body, D1) ->
            assertz(coverage_log(Name, Arity, succeeded))
        ;
            assertz(coverage_log(Name, Arity, subgoal_failed)),
            assertz(failed_subgoal(Goal)),
            fail
        )
    ;
        assertz(coverage_log(Name, Arity, unify_failed)),
        assertz(failed_subgoal(Goal)),
        fail
    ).
solve(Goal, 0) :-
    functor(Goal, Name, Arity),
    assertz(coverage_log(Name, Arity, depth_exceeded)),
    fail.
"""

_META_INTERP_FILE: str | None = None
_prolog_instance = None


def _ensure_meta_interp_file() -> str:
    global _META_INTERP_FILE
    if _META_INTERP_FILE and Path(_META_INTERP_FILE).exists():
        return _META_INTERP_FILE
    tf = tempfile.NamedTemporaryFile(suffix=".pl", mode="w", delete=False, dir="/tmp")
    tf.write(META_INTERPRETER_PROLOG)
    tf.flush()
    tf.close()
    _META_INTERP_FILE = tf.name
    return _META_INTERP_FILE


PROLOG_AVAILABLE = False


def get_prolog(force_new: bool = False):
    global _prolog_instance, PROLOG_AVAILABLE
    if _prolog_instance is None or force_new:
        try:
            from pyswip import Prolog  # type: ignore
            p = Prolog()
            try:
                list(p.query("set_prolog_flag(stack_limit, 268435456)"))
            except Exception:
                pass
            mf = _ensure_meta_interp_file()
            try:
                list(p.query(f'consult("{mf}")'))
            except Exception as e:
                logger.warning(f"Meta-interp load: {e}")
            _prolog_instance = p
            PROLOG_AVAILABLE = True
            logger.info("Prolog instance initialized")
        except Exception as e:
            logger.warning(f"Prolog not available: {e}. Using fallback.")
            _prolog_instance = None
            PROLOG_AVAILABLE = False
    return _prolog_instance


def reset_prolog() -> None:
    global _prolog_instance
    _prolog_instance = None
    gc.collect()


_PREDICATE_SIGNATURE_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*/\d+$")
_PROLOG_DANGEROUS_RE = re.compile(r"\b(assert|retract|abolish|consult|halt|write|read|open|close|nl)\b")


def sanitize_clause(clause: str) -> str | None:
    c = clause.strip().rstrip(".")
    if not c:
        return None
    if _PREDICATE_SIGNATURE_RE.match(c):
        return None
    if _PROLOG_DANGEROUS_RE.search(c):
        return None
    c = re.sub(r"\s*%.*$", "", c).strip()
    c = re.sub(r"\bnot\s*\(", r"\\+(", c)
    if re.search(r"\bnot\s+[a-zA-Z_]", c):
        return None
    if c.lstrip().startswith("\\+"):
        return None
    head_part = c.split(":-")[0].strip()
    depth = 0
    for ch in head_part:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            return None
    if c.startswith("/*") or c.startswith("*/"):
        return None
    if re.match(r"^\d+\.", c) or "`" in c or c.startswith("-") or c.startswith("*"):
        return None
    if re.match(r"^[A-Z]", c.split(":-")[0].strip()):
        return None
    c = c.strip()
    if not c:
        return None
    return c + "."


def extract_predicate_names_from_clauses(clauses: list[str]) -> list[tuple[str, int]]:
    preds: set[tuple[str, int]] = set()
    for clause in clauses:
        head = clause.split(":-")[0].strip().rstrip(".")
        m = re.match(r"([a-z][a-zA-Z0-9_]*)\((.*)\)", head, re.DOTALL)
        if m:
            name = m.group(1)
            args_str = m.group(2).strip()
            depth = 0
            arity = 1 if args_str else 0
            for ch in args_str:
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                elif ch == "," and depth == 0:
                    arity += 1
            preds.add((name, arity))
        else:
            plain = re.match(r"([a-z][a-zA-Z0-9_]*)$", head)
            if plain:
                preds.add((plain.group(1), 0))
    return list(preds)


def load_kb_into_prolog(prolog, clauses: list[str]) -> list[str]:
    ok: list[str] = []
    for clause in clauses:
        safe = sanitize_clause(clause)
        if safe is None:
            continue
        c = safe.rstrip(".")
        try:
            prolog.assertz(c)
            ok.append(safe)
        except Exception as e:
            logger.debug(f"assertz failed: {str(e)[:50]}")
    return ok


def clear_kb(prolog, pred_names: list[tuple[str, int]]) -> None:
    builtin_skip = {
        "true", "fail", "false", "nl", "write", "writeln", "assert", "retract",
        "solve", "reset_coverage", "coverage_log", "failed_subgoal",
    }
    for name, arity in pred_names:
        if name in builtin_skip:
            continue
        try:
            _prolog_query_with_timeout(prolog, f"abolish({name}/{arity})", timeout_sec=2)
        except Exception:
            pass


def _prolog_query_with_timeout(prolog, query_str: str, timeout_sec: int = 3) -> list:
    wrapped = f"catch(call_with_time_limit({timeout_sec}, ({query_str})), time_limit_exceeded, fail)"
    try:
        return list(prolog.query(wrapped))
    except Exception as e:
        logger.debug(f"Prolog query error: {str(e)[:60]}")
        return []


def run_oracle_query_with_coverage(prolog, goal_str: str, depth: int = 2) -> dict[str, Any]:
    try:
        _prolog_query_with_timeout(prolog, "reset_coverage", timeout_sec=3)
    except Exception:
        pass
    succeeded = False
    try:
        results = _prolog_query_with_timeout(
            prolog, f"solve_safe(({goal_str}), {depth})", timeout_sec=5
        )
        succeeded = len(results) > 0
    except Exception:
        pass
    coverage: dict[str, list[str]] = {}
    try:
        for sol in _prolog_query_with_timeout(prolog, "coverage_log(N, A, O)", timeout_sec=3):
            key = f"{sol['N']}/{sol['A']}"
            coverage.setdefault(key, []).append(str(sol["O"]))
    except Exception:
        pass
    failed_subgoals: list[str] = []
    try:
        for sol in _prolog_query_with_timeout(prolog, "failed_subgoal(G)", timeout_sec=3):
            failed_subgoals.append(str(sol["G"]))
    except Exception:
        pass
    return {"succeeded": succeeded, "coverage": coverage, "failed_subgoals": failed_subgoals}


# ── Prompts ───────────────────────────────────────────────────────────────────
EXTRACTION_PROMPT = """\
You are a Prolog knowledge base extractor. Given natural language premises, \
extract a Prolog knowledge base (facts and rules).

Rules:
- Use snake_case predicate names (e.g., is_happy/1, parent_of/2)
- Each fact/rule on its own line ending with '.'
- No comments, no module declarations, no :- use_module lines
- Use only alphanumeric and underscore in predicate/atom names
- Atoms that are proper nouns must be lowercase (e.g., john, bonnie)
- Output ONLY the Prolog code, nothing else

Premises:
{premises}

Prolog KB:"""

ORACLE_PROMPT = """\
Given these premises, generate {n} yes/no reasoning questions that test whether \
specific facts from the premises hold. Generate a mix: roughly half answerable 'yes', \
half 'no'. Format: one question per line, starting with 'Q: '.

Premises:
{premises}

Questions:"""

ANSWER_PROMPT = """\
Based on the premises, answer each question with 'yes' or 'no'. \
Output ONLY 'yes' or 'no' for each question, one answer per line.

Premises:
{premises}

Questions:
{questions}

Answers (one per line):"""

BATCH_NL_TO_PROLOG_PROMPT = """\
Convert each yes/no question to a Prolog goal (without '?-').
Use ONLY predicates listed. Output one goal per line (no numbering, no explanation).
If a question cannot be converted, output: fail

Available KB predicates: {predicates}

Questions:
{questions}

Prolog goals (one per line):"""

CONCLUSION_EVAL_PROMPT = """\
Convert this natural language conclusion to a Prolog goal (without '?-').
Use ONLY predicates listed below. Output exactly one line: the Prolog goal expression.
No explanations, no punctuation except the goal itself.
If it cannot be converted, output exactly: fail

Conclusion: {conclusion_text}

Available KB predicates: {predicates}

Prolog goal (single line only):"""

KB_EVAL_PROMPT = """\
Given this knowledge base of facts and rules:
{kb}

Question: Is the following conclusion True or False based strictly on the knowledge base?
Conclusion: {conclusion}

Think step by step, then answer with exactly one word: True or False."""

ORACLE_KB_EVAL_PROMPT = """\
Given this knowledge base of facts and rules:
{kb}

Answer each question with 'yes' or 'no' based on the knowledge base.
Output ONLY 'yes' or 'no' for each question, one per line.

Questions:
{questions}

Answers (one per line):"""

REPAIR_PROMPT = """\
Repair this Prolog KB extracted from natural language. The predicate/sub-goal below \
is suspected erroneous or missing.

Premises:
{premises}

Current Prolog KB:
{kb}

Suspicious item: {item}
Reason: {reason}

Provide corrected/new Prolog clause(s). Output ONLY valid Prolog clauses, one per line, \
ending with '.'. No explanations."""

COT_PROMPT = """\
Given the following premises:
{premises}

Conclusion: {conclusion}

Think step by step, then answer with exactly one word: True or False."""

SELFREFINE_REFINE_PROMPT = """\
Your Prolog KB achieved {pass_rate:.0%} on oracle queries.
Premises:
{premises}

Current KB:
{kb}

Regenerate the KB to improve coverage. Output ONLY Prolog clauses."""


# ── Pipeline functions ────────────────────────────────────────────────────────
def _parse_clauses_from_text(text: str) -> list[str]:
    lines = text.split("\n")
    result = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("%") or ln.startswith(":-"):
            continue
        if ln.endswith("."):
            c = sanitize_clause(ln)
            if c:
                result.append(c)
    return result


def extract_kb(premises_text: str) -> list[str]:
    response, _ = llm_call(
        [{"role": "user", "content": EXTRACTION_PROMPT.format(premises=premises_text[:2000])}],
        max_tokens=800,
    )
    return _parse_clauses_from_text(response)


def generate_oracle_queries(premises_text: str, n: int = N_ORACLE_QUERIES) -> list[str]:
    response, _ = llm_call(
        [{"role": "user", "content": ORACLE_PROMPT.format(premises=premises_text[:2000], n=n)}],
        max_tokens=400,
    )
    lines = [
        ln.strip().lstrip("Q:").lstrip("q:").strip()
        for ln in response.split("\n")
        if ln.strip().upper().startswith("Q:")
    ]
    return lines[:n]


def generate_oracle_answers(premises_text: str, questions: list[str]) -> list[str]:
    q_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    response, _ = llm_call(
        [{"role": "user", "content": ANSWER_PROMPT.format(
            premises=premises_text[:2000], questions=q_block
        )}],
        max_tokens=200,
    )
    raw_lines = [ln.strip().lower() for ln in response.split("\n") if ln.strip()]
    answers: list[str] = []
    for ln in raw_lines:
        for word in re.split(r"[^a-z]+", ln):
            if word in ("yes", "no", "unknown"):
                answers.append(word)
                break
        if len(answers) == len(questions):
            break
    while len(answers) < len(questions):
        answers.append("unknown")
    return answers[:len(questions)]


def sanitize_prolog_goal(goal: str) -> str:
    if not goal:
        return "fail"
    goal = re.sub(r"\bnot\s*\(", r"\\+(", goal)
    if re.search(r"\bnot\s+[a-zA-Z_]", goal):
        return "fail"
    if re.search(r"[~@#$]", goal):
        return "fail"
    dangerous = ["assert", "retract", "abolish", "consult", "halt", "write", "nl"]
    if any(d in goal.lower() for d in dangerous):
        return "fail"
    goal = re.sub(r'\b([A-Z][a-zA-Z0-9_]*)\s*\(', lambda m: m.group(1).lower() + '(', goal)
    if re.match(r'^[^a-z\(\\]', goal.strip()):
        return "fail"
    return goal


def nl_queries_to_prolog_batch(questions: list[str], pred_sigs: list[str]) -> list[str]:
    if not questions:
        return []
    sig_block = ", ".join(pred_sigs[:30])
    q_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    response, _ = llm_call(
        [{"role": "user", "content": BATCH_NL_TO_PROLOG_PROMPT.format(
            predicates=sig_block, questions=q_block
        )}],
        max_tokens=300, temperature=0.0,
    )
    lines = [ln.strip().rstrip(".") for ln in response.split("\n") if ln.strip()]
    goals = []
    for ln in lines:
        if re.match(r"^\d+\.", ln):
            parts = ln.split(".", 1)
            if len(parts) > 1:
                ln = parts[1].strip()
        if ln and not ln.startswith("#"):
            goals.append(sanitize_prolog_goal(ln))
    while len(goals) < len(questions):
        goals.append("fail")
    return goals[:len(questions)]


def evaluate_conclusion_proofwriter(
    prolog, conclusion_nl: str, kb_clauses: list[str], pred_sigs: list[str]
) -> str:
    """Evaluate ProofWriter conclusion. Uses Prolog if available, else LLM."""
    if not conclusion_nl:
        return "Uncertain"

    if prolog is None or not PROLOG_AVAILABLE:
        return evaluate_conclusion_llm(conclusion_nl, kb_clauses)

    sig_block = ", ".join(pred_sigs[:30])
    response, _ = llm_call(
        [{"role": "user", "content": CONCLUSION_EVAL_PROMPT.format(
            conclusion_text=conclusion_nl, predicates=sig_block
        )}],
        max_tokens=100,
    )
    goal = ""
    for ln in response.split("\n"):
        ln = ln.strip().rstrip(".")
        if ln and re.match(r'^[a-z\(\\]', ln) and len(ln) <= 200:
            goal = ln
            break
    if not goal or goal.lower() in ("fail", "false"):
        return evaluate_conclusion_llm(conclusion_nl, kb_clauses)
    goal = sanitize_prolog_goal(goal)
    if goal == "fail":
        return evaluate_conclusion_llm(conclusion_nl, kb_clauses)
    # Run Prolog query
    try:
        wrapped_pos = f"catch(call_with_depth_limit(({goal}), 20, _), _, fail)"
        pos = _prolog_query_with_timeout(prolog, wrapped_pos, timeout_sec=2)
        if pos:
            neg = _prolog_query_with_timeout(
                prolog, f"catch(call_with_depth_limit((\\+({goal})), 20, _), _, fail)", timeout_sec=2
            )
            return "False" if neg else "True"
        else:
            neg = _prolog_query_with_timeout(
                prolog, f"catch(call_with_depth_limit((\\+({goal})), 20, _), _, fail)", timeout_sec=2
            )
            return "False" if neg else "Uncertain"
    except Exception as e:
        logger.debug(f"evaluate_conclusion Prolog failed: {e}, falling back to LLM")
        return evaluate_conclusion_llm(conclusion_nl, kb_clauses)


def evaluate_conclusion_llm(conclusion_nl: str, kb_clauses: list[str]) -> str:
    """LLM-only fallback: evaluate conclusion from KB text."""
    kb_str = "\n".join(kb_clauses[:50]) if kb_clauses else "(empty knowledge base)"
    try:
        response, _ = llm_call(
            [{"role": "user", "content": KB_EVAL_PROMPT.format(
                kb=kb_str[:2000], conclusion=conclusion_nl
            )}],
            max_tokens=200,
        )
    except BudgetExceededError:
        raise
    except Exception:
        return "Uncertain"
    for word in reversed(response.split()):
        w = word.strip(".,!?:").capitalize()
        if w in ("True", "False"):
            return w
    return "Uncertain"


def compute_ochiai_scores(
    coverage_matrix: dict[str, list[str | None]], pass_fail: list[bool]
) -> dict[str, float]:
    scores: dict[str, float] = {}
    n_failing = sum(1 for p in pass_fail if not p)
    for pred_key, outcomes in coverage_matrix.items():
        if not any(o == "unify_failed" for o in outcomes if o):
            continue
        ef = sum(1 for i, o in enumerate(outcomes) if o is not None and not pass_fail[i])
        ep = sum(1 for i, o in enumerate(outcomes) if o is not None and pass_fail[i])
        nf = n_failing - ef
        denom = math.sqrt((ef + nf) * (ef + ep))
        scores[pred_key] = ef / denom if denom > 0 else 0.0
    return scores


def compute_missing_predicate_scores(
    all_failed_subgoals: list[str], kb_pred_keys: set[str]
) -> dict[str, int]:
    counts: Counter = Counter()
    for subgoal in all_failed_subgoals:
        m = re.match(r"([a-z][a-zA-Z0-9_]*).*", subgoal)
        if m:
            pred_name = m.group(1)
            in_kb = any(k.split("/")[0] == pred_name for k in kb_pred_keys)
            if not in_kb:
                counts[subgoal] += 1
    return dict(counts)


def build_repair_agenda(
    ochiai_scores: dict[str, float],
    missing_scores: dict[str, int],
    k: int = K_REPAIR_TARGETS,
) -> list[dict[str, Any]]:
    max_o = max(ochiai_scores.values(), default=1.0) or 1.0
    max_m = max(missing_scores.values(), default=1) or 1
    agenda: list[dict] = []
    for pred, score in ochiai_scores.items():
        agenda.append({"item": pred, "score": score / max_o, "type": "erroneous"})
    for subgoal, freq in missing_scores.items():
        agenda.append({"item": subgoal, "score": freq / max_m, "type": "missing"})
    agenda.sort(key=lambda x: x["score"], reverse=True)
    return agenda[:k]


def repair_item(premises_text: str, kb_clauses: list[str], item: str, reason: str) -> list[str]:
    kb_str = "\n".join(kb_clauses[:40])
    response, _ = llm_call(
        [{"role": "user", "content": REPAIR_PROMPT.format(
            premises=premises_text[:1500], kb=kb_str, item=item, reason=reason
        )}],
        max_tokens=400,
    )
    return _parse_clauses_from_text(response)


# ── Main pipeline for ProofWriter ─────────────────────────────────────────────
def run_dual_sbfl_proofwriter(
    premises_text: str,
    conclusion_nl: str,
    prolog,
    flat_mode: bool = False,
    precomputed: dict | None = None,
) -> dict[str, Any]:
    all_preds: list[tuple[str, int]] = []
    num_repairs = 0

    # Use precomputed KB/oracle artifacts if provided (saves LLM calls for flat_sbfl)
    if precomputed is not None:
        clauses = list(precomputed.get("clauses", []))
        oracle_questions = precomputed.get("oracle_questions", [])
        oracle_answers = precomputed.get("oracle_answers", [])
        prolog_goals = precomputed.get("prolog_goals", [])
    else:
        try:
            clauses = extract_kb(premises_text)
        except BudgetExceededError:
            raise
        except Exception as e:
            logger.warning(f"KB extraction failed: {e}")
            clauses = []

        try:
            oracle_questions = generate_oracle_queries(premises_text)
        except BudgetExceededError:
            raise
        except Exception:
            oracle_questions = []

        try:
            oracle_answers = generate_oracle_answers(premises_text, oracle_questions) if oracle_questions else []
        except BudgetExceededError:
            raise
        except Exception:
            oracle_answers = ["unknown"] * len(oracle_questions)

        pred_names = extract_predicate_names_from_clauses(clauses)
        pred_sigs = [f"{n}/{a}" for n, a in pred_names]
        try:
            prolog_goals = nl_queries_to_prolog_batch(oracle_questions, pred_sigs)
        except BudgetExceededError:
            raise
        except Exception:
            prolog_goals = ["fail"] * len(oracle_questions)

    pass_fail = [a == "yes" for a in oracle_answers]
    pred_names = extract_predicate_names_from_clauses(clauses)
    pred_sigs = [f"{n}/{a}" for n, a in pred_names]

    current_clauses = clauses[:]
    final_ochiai: dict[str, float] = {}
    final_missing: dict[str, int] = {}

    if prolog is not None and PROLOG_AVAILABLE:
        for round_idx in range(N_REPAIR_ROUNDS):
            if COST_TRACKER["total"] >= MAX_BUDGET_USD:
                break
            if not current_clauses:
                break

            pred_names_cur = extract_predicate_names_from_clauses(current_clauses)
            clear_kb(prolog, all_preds)
            load_kb_into_prolog(prolog, current_clauses)
            all_preds = pred_names_cur
            n_queries = len(prolog_goals)
            coverage_matrix: dict[str, list[str | None]] = {}
            all_failed_subgoals: list[str] = []

            for qi, goal in enumerate(prolog_goals):
                if goal == "fail":
                    continue
                qresult = run_oracle_query_with_coverage(prolog, goal)
                all_failed_subgoals.extend(qresult["failed_subgoals"])
                for pred_key, outcomes in qresult["coverage"].items():
                    if pred_key not in coverage_matrix:
                        coverage_matrix[pred_key] = [None] * n_queries
                    worst = outcomes[-1] if outcomes else None
                    coverage_matrix[pred_key][qi] = worst

            if flat_mode:
                flat_ochiai: dict[str, float] = {}
                n_failing = sum(1 for p in pass_fail if not p)
                for pred_key, outcomes in coverage_matrix.items():
                    ef = sum(1 for ii, o in enumerate(outcomes) if o is not None and not pass_fail[ii])
                    ep = sum(1 for ii, o in enumerate(outcomes) if o is not None and pass_fail[ii])
                    nf = n_failing - ef
                    denom = math.sqrt((ef + nf) * (ef + ep))
                    flat_ochiai[pred_key] = ef / denom if denom > 0 else 0.0
                ochiai = flat_ochiai
                missing: dict[str, int] = {}
            else:
                ochiai = compute_ochiai_scores(coverage_matrix, pass_fail)
                kb_pred_keys = set(coverage_matrix.keys())
                missing = compute_missing_predicate_scores(all_failed_subgoals, kb_pred_keys)

            final_ochiai = ochiai
            final_missing = missing
            agenda = build_repair_agenda(ochiai, missing, k=K_REPAIR_TARGETS)
            if not agenda:
                break

            for item_info in agenda:
                if COST_TRACKER["total"] >= MAX_BUDGET_USD:
                    break
                try:
                    new_clauses = repair_item(
                        premises_text, current_clauses, item_info["item"],
                        f"Type: {item_info['type']}, Score: {item_info['score']:.3f}",
                    )
                except BudgetExceededError:
                    raise
                except Exception as e:
                    logger.warning(f"Repair failed: {e}")
                    continue
                if new_clauses:
                    num_repairs += 1
                    if item_info["type"] == "erroneous":
                        pred_name = item_info["item"].split("/")[0]
                        current_clauses = [
                            c for c in current_clauses
                            if not re.match(r"^" + re.escape(pred_name) + r"[\s(]", c.split(":-")[0].strip())
                        ]
                    current_clauses.extend(new_clauses)
    else:
        # LLM-based fallback for SBFL (no Prolog): use oracle answers vs KB to rank predicates
        for round_idx in range(N_REPAIR_ROUNDS):
            if COST_TRACKER["total"] >= MAX_BUDGET_USD:
                break
            if not current_clauses or not oracle_questions:
                break
            # Evaluate oracle questions against KB via LLM
            try:
                kb_str = "\n".join(current_clauses[:40])
                q_block = "\n".join(f"{ii+1}. {q}" for ii, q in enumerate(oracle_questions))
                kb_oracle_resp, _ = llm_call(
                    [{"role": "user", "content": ORACLE_KB_EVAL_PROMPT.format(
                        kb=kb_str[:2000], questions=q_block
                    )}], max_tokens=200,
                )
                kb_answers = []
                for ln in kb_oracle_resp.split("\n"):
                    ln = ln.strip().lower()
                    for word in re.split(r"[^a-z]+", ln):
                        if word in ("yes", "no"):
                            kb_answers.append(word)
                            break
                while len(kb_answers) < len(oracle_questions):
                    kb_answers.append("unknown")
                kb_answers = kb_answers[:len(oracle_questions)]
            except BudgetExceededError:
                raise
            except Exception:
                break

            # Find disagreements (oracle says X but KB says Y)
            disagreements = [
                oracle_questions[ii] for ii, (oa, ka) in enumerate(zip(oracle_answers, kb_answers))
                if oa != ka and oa in ("yes", "no")
            ]

            if not disagreements:
                break

            # Keyword-based predicate ranking: predicates whose names appear in disagreement questions
            pred_names_cur = extract_predicate_names_from_clauses(current_clauses)
            pred_scores: dict[str, float] = {}
            for pred_name, _ in pred_names_cur:
                score = sum(1.0 for dq in disagreements if pred_name.replace("_", " ") in dq.lower())
                if score > 0:
                    pred_scores[f"{pred_name}"] = score

            if flat_mode:
                missing_llm: dict[str, int] = {}
            else:
                # LLM sub-goal harvesting: find predicates mentioned in disagreement questions not in KB
                all_question_words = set()
                for dq in disagreements:
                    all_question_words.update(re.findall(r'\b[a-z]{4,}\b', dq.lower()))
                kb_pred_set = {n for n, _ in pred_names_cur}
                missing_llm = {
                    w: 1 for w in all_question_words
                    if w not in kb_pred_set and w not in {
                        "this", "that", "does", "have", "will", "when", "what", "from",
                        "with", "into", "them", "they", "their", "also", "both", "each"
                    }
                }

            final_ochiai = {k: v / max(len(disagreements), 1) for k, v in pred_scores.items()}
            final_missing = missing_llm

            agenda = build_repair_agenda(final_ochiai, final_missing, k=K_REPAIR_TARGETS)
            if not agenda:
                break

            for item_info in agenda:
                if COST_TRACKER["total"] >= MAX_BUDGET_USD:
                    break
                try:
                    new_clauses = repair_item(
                        premises_text, current_clauses, item_info["item"],
                        f"Type: {item_info['type']}, Score: {item_info['score']:.3f}",
                    )
                except BudgetExceededError:
                    raise
                except Exception as e:
                    logger.warning(f"Repair failed: {e}")
                    continue
                if new_clauses:
                    num_repairs += 1
                    if item_info["type"] == "erroneous":
                        pred_name = item_info["item"].split("/")[0]
                        current_clauses = [
                            c for c in current_clauses
                            if not re.match(r"^" + re.escape(pred_name) + r"[\s(]", c.split(":-")[0].strip())
                        ]
                    current_clauses.extend(new_clauses)

    pred_names_final = extract_predicate_names_from_clauses(current_clauses)
    if prolog is not None and PROLOG_AVAILABLE:
        clear_kb(prolog, all_preds)
        load_kb_into_prolog(prolog, current_clauses)
        all_preds = pred_names_final
        pred_sigs_final = [f"{n}/{a}" for n, a in pred_names_final]
        try:
            predicted = evaluate_conclusion_proofwriter(
                prolog, conclusion_nl, current_clauses, pred_sigs_final
            )
        except BudgetExceededError:
            raise
        except Exception:
            predicted = evaluate_conclusion_llm(conclusion_nl, current_clauses)
        clear_kb(prolog, all_preds)
    else:
        predicted = evaluate_conclusion_llm(conclusion_nl, current_clauses)

    return {
        "predicted_label": predicted,
        "num_repairs": num_repairs,
        "kb_size": len(current_clauses),
        "kb_clauses": current_clauses,
        "ochiai_top5": sorted(final_ochiai.items(), key=lambda x: -x[1])[:5],
        "subgoal_top5": sorted(final_missing.items(), key=lambda x: -x[1])[:5] if not flat_mode else [],
        "precomputed": {
            "clauses": clauses,
            "oracle_questions": oracle_questions,
            "oracle_answers": oracle_answers,
            "prolog_goals": prolog_goals,
        },
    }


def run_oneshot_proofwriter(premises_text: str, conclusion_nl: str, prolog) -> str:
    try:
        clauses = extract_kb(premises_text)
    except BudgetExceededError:
        raise
    except Exception:
        clauses = []
    if prolog is None or not PROLOG_AVAILABLE:
        return evaluate_conclusion_llm(conclusion_nl, clauses)
    pred_names = extract_predicate_names_from_clauses(clauses)
    load_kb_into_prolog(prolog, clauses)
    pred_sigs = [f"{n}/{a}" for n, a in pred_names]
    try:
        predicted = evaluate_conclusion_proofwriter(prolog, conclusion_nl, clauses, pred_sigs)
    except BudgetExceededError:
        raise
    except Exception:
        predicted = evaluate_conclusion_llm(conclusion_nl, clauses)
    clear_kb(prolog, pred_names)
    return predicted


def run_cot_proofwriter(premises_text: str, conclusion_nl: str) -> str:
    try:
        response, _ = llm_call(
            [{"role": "user", "content": COT_PROMPT.format(
                premises=premises_text[:2000], conclusion=conclusion_nl
            )}], max_tokens=400,
        )
    except BudgetExceededError:
        raise
    except Exception:
        return "Uncertain"
    for word in reversed(response.split()):
        w = word.strip(".,!?:").capitalize()
        if w in ("True", "False", "Uncertain"):
            return w
    return "Uncertain"


def run_selfrefine_proofwriter(premises_text: str, conclusion_nl: str, prolog) -> str:
    try:
        oracle_qs = generate_oracle_queries(premises_text, n=8)
        oracle_as = generate_oracle_answers(premises_text, oracle_qs)
        clauses = extract_kb(premises_text)
    except BudgetExceededError:
        raise
    except Exception:
        return "Uncertain"

    all_preds: list[tuple[str, int]] = []
    for r in range(N_REPAIR_ROUNDS):
        if COST_TRACKER["total"] >= MAX_BUDGET_USD:
            break
        pred_names = extract_predicate_names_from_clauses(clauses)
        if prolog is not None and PROLOG_AVAILABLE:
            clear_kb(prolog, all_preds)
            load_kb_into_prolog(prolog, clauses)
            all_preds = pred_names
            pred_sigs = [f"{n}/{a}" for n, a in pred_names]
            goals = nl_queries_to_prolog_batch(oracle_qs, pred_sigs)
            passed = 0
            for qi, goal in enumerate(goals):
                if goal == "fail":
                    continue
                try:
                    wrapped = f"catch(call_with_depth_limit(({goal}), 20, _), _, fail)"
                    res = _prolog_query_with_timeout(prolog, wrapped, timeout_sec=2)
                    if (res and oracle_as[qi] == "yes") or (not res and oracle_as[qi] != "yes"):
                        passed += 1
                except Exception:
                    pass
            pass_rate = passed / max(len(oracle_qs), 1)
        else:
            # LLM-based pass-rate estimation
            try:
                kb_str = "\n".join(clauses[:40])
                q_block = "\n".join(f"{qi+1}. {q}" for qi, q in enumerate(oracle_qs))
                kb_resp, _ = llm_call(
                    [{"role": "user", "content": ORACLE_KB_EVAL_PROMPT.format(
                        kb=kb_str[:2000], questions=q_block
                    )}], max_tokens=200,
                )
                kb_ans = []
                for ln in kb_resp.split("\n"):
                    for word in re.split(r"[^a-z]+", ln.strip().lower()):
                        if word in ("yes", "no"):
                            kb_ans.append(word)
                            break
                while len(kb_ans) < len(oracle_qs):
                    kb_ans.append("unknown")
                passed = sum(1 for oa, ka in zip(oracle_as, kb_ans) if oa == ka)
                pass_rate = passed / max(len(oracle_qs), 1)
            except BudgetExceededError:
                raise
            except Exception:
                pass_rate = 0.5
        try:
            response, _ = llm_call(
                [{"role": "user", "content": SELFREFINE_REFINE_PROMPT.format(
                    pass_rate=pass_rate, premises=premises_text[:1500],
                    kb="\n".join(clauses[:40])
                )}], max_tokens=600,
            )
            new_clauses = _parse_clauses_from_text(response)
            if new_clauses:
                clauses = new_clauses
        except BudgetExceededError:
            raise
        except Exception:
            pass

    pred_names = extract_predicate_names_from_clauses(clauses)
    if prolog is not None and PROLOG_AVAILABLE:
        clear_kb(prolog, all_preds)
        load_kb_into_prolog(prolog, clauses)
        all_preds = pred_names
        pred_sigs = [f"{n}/{a}" for n, a in pred_names]
        try:
            predicted = evaluate_conclusion_proofwriter(prolog, conclusion_nl, clauses, pred_sigs)
        except BudgetExceededError:
            raise
        except Exception:
            predicted = evaluate_conclusion_llm(conclusion_nl, clauses)
        clear_kb(prolog, all_preds)
    else:
        predicted = evaluate_conclusion_llm(conclusion_nl, clauses)
    return predicted


# ── Hop depth estimation ──────────────────────────────────────────────────────
def estimate_hop_depth(premises_text: str) -> int:
    rule_indicators = ['if ', 'all ', 'every ', 'anyone who', 'anyone that']
    count = sum(premises_text.lower().count(ind) for ind in rule_indicators)
    if count <= 1:
        return 1
    elif count <= 3:
        return 2
    else:
        return 3


# ── Pseudo-gold KB extraction for fuzzy/strict recall ────────────────────────
def extract_pseudo_gold_predicates(premises_text: str) -> set[str]:
    """Extract predicate names from NL text as pseudo-gold using pattern matching."""
    preds = set()
    # Pattern: "X is Y" → is_y or just y
    for m in re.finditer(r'\b(\w+)\s+is\s+(a\s+)?(\w+)', premises_text, re.IGNORECASE):
        preds.add(m.group(3).lower())
        preds.add(f"is_{m.group(3).lower()}")
    # Pattern: "X verb Y" (simple actions)
    action_verbs = ['chases', 'visits', 'sees', 'likes', 'eats', 'knows', 'helps', 'needs']
    for verb in action_verbs:
        if verb in premises_text.lower():
            preds.add(verb.rstrip('s'))  # normalize
            preds.add(verb)
    # Pattern: conditional predicates from "If X then Y" or "All X are Y"
    for m in re.finditer(r'(?:if|all|every)\s+\w+\s+(?:is|are)\s+(a\s+)?(\w+)', premises_text, re.IGNORECASE):
        preds.add(m.group(2).lower())
    # Extract all content words as potential predicate names (fallback)
    words = re.findall(r'\b[a-z]{3,}\b', premises_text.lower())
    stopwords = {'the', 'and', 'are', 'is', 'if', 'all', 'that', 'then', 'not', 'who',
                 'can', 'has', 'have', 'was', 'were', 'will', 'this', 'with', 'from',
                 'for', 'but', 'its', 'they', 'their', 'also', 'when', 'what', 'than',
                 'each', 'any', 'both', 'very', 'just', 'into', 'over', 'such', 'some'}
    for w in words:
        if w not in stopwords and len(w) >= 4:
            preds.add(w)
    return preds


def jaccard_similarity(a: str, b: str) -> float:
    toks_a = set(a.lower().split('_') + [a.lower()])
    toks_b = set(b.lower().split('_') + [b.lower()])
    inter = len(toks_a & toks_b)
    union = len(toks_a | toks_b)
    return inter / union if union > 0 else 0.0


def compute_kb_precision_recall(
    kb_clauses: list[str], premises_text: str, fuzzy_thresh: float = 0.5, strict_thresh: float = 0.3
) -> dict[str, float]:
    """Compute strict and fuzzy precision/recall for KB predicates vs pseudo-gold."""
    kb_preds = [n for n, _ in extract_predicate_names_from_clauses(kb_clauses)]
    gold_preds = extract_pseudo_gold_predicates(premises_text)

    if not kb_preds or not gold_preds:
        return {"strict_recall": 0.0, "fuzzy_recall": 0.0, "strict_precision": 0.0, "fuzzy_precision": 0.0}

    gold_preds_list = list(gold_preds)
    # Normalize to snake_case
    def normalize(s: str) -> str:
        return re.sub(r'[^a-z0-9]', '_', s.lower())

    kb_norm = [normalize(p) for p in kb_preds]
    gold_norm = [normalize(p) for p in gold_preds_list]

    # Strict recall: fraction of gold preds that appear verbatim in KB
    strict_recall = sum(1 for g in gold_norm if g in kb_norm) / len(gold_norm)

    # Fuzzy recall: fraction of gold preds with Jaccard >= thresh to any KB pred
    fuzzy_recall = sum(
        1 for g in gold_norm if any(jaccard_similarity(g, k) >= fuzzy_thresh for k in kb_norm)
    ) / len(gold_norm)

    # Strict precision: fraction of KB preds that appear verbatim in gold
    strict_precision = sum(1 for k in kb_norm if k in gold_norm) / max(len(kb_norm), 1)

    # Fuzzy precision
    fuzzy_precision = sum(
        1 for k in kb_norm if any(jaccard_similarity(k, g) >= fuzzy_thresh for g in gold_norm)
    ) / max(len(kb_norm), 1)

    return {
        "strict_recall": strict_recall,
        "fuzzy_recall": fuzzy_recall,
        "strict_precision": strict_precision,
        "fuzzy_precision": fuzzy_precision,
    }


def compute_hallucination_rate(kb_clauses: list[str], premises_text: str) -> float:
    """Fraction of KB predicates that can't be fuzzy-matched (Jaccard < 0.3) to source text."""
    kb_preds = [n for n, _ in extract_predicate_names_from_clauses(kb_clauses)]
    if not kb_preds:
        return 0.0
    all_words = set(re.findall(r'\b[a-z]{3,}\b', premises_text.lower()))
    hallucinated = 0
    for pred in kb_preds:
        parts = pred.lower().split('_')
        matched = any(jaccard_similarity(pred.lower(), w) >= 0.3 for w in all_words)
        if not matched:
            hallucinated += 1
    return hallucinated / len(kb_preds)


# ── Bootstrap CI ──────────────────────────────────────────────────────────────
def bootstrap_ci_diff(
    metric_a: np.ndarray, metric_b: np.ndarray, n_resamples: int = N_BOOTSTRAP, ci: float = 0.95
) -> tuple[float, float]:
    diffs = []
    n = len(metric_a)
    rng = np.random.default_rng(42)
    for _ in range(n_resamples):
        idx = rng.integers(0, n, n)
        diffs.append(float(np.mean(metric_a[idx]) - np.mean(metric_b[idx])))
    alpha = (1 - ci) / 2
    return (float(np.percentile(diffs, alpha * 100)), float(np.percentile(diffs, (1 - alpha) * 100)))


def cohens_h(p1: float, p2: float) -> float:
    """Cohen's h effect size for two proportions."""
    phi1 = 2 * math.asin(math.sqrt(max(0.0, min(1.0, p1))))
    phi2 = 2 * math.asin(math.sqrt(max(0.0, min(1.0, p2))))
    return abs(phi1 - phi2)


def bootstrap_ci_mean(metric: np.ndarray, n_resamples: int = N_BOOTSTRAP, ci: float = 0.95) -> tuple[float, float]:
    means = []
    n = len(metric)
    rng = np.random.default_rng(42)
    for _ in range(n_resamples):
        idx = rng.integers(0, n, n)
        means.append(float(np.mean(metric[idx])))
    alpha = (1 - ci) / 2
    return (float(np.percentile(means, alpha * 100)), float(np.percentile(means, (1 - alpha) * 100)))


# ── Perturbation calibration ──────────────────────────────────────────────────
def inject_corruptions(kb_clauses: list[str], n_corrupt: int = 5, seed: int = 42) -> tuple[list[str], list[int]]:
    """Inject n_corrupt synthetic errors into KB. Returns (corrupted_clauses, corrupted_indices)."""
    rng = random.Random(seed)
    # Find fact clauses (no :- body) that have arguments
    fact_indices = [i for i, c in enumerate(kb_clauses) if ':-' not in c and '(' in c]
    if len(fact_indices) < n_corrupt:
        fact_indices = list(range(len(kb_clauses)))
    chosen = rng.sample(fact_indices, min(n_corrupt, len(fact_indices)))

    corrupted = list(kb_clauses)
    for idx in chosen:
        clause = corrupted[idx]
        # Corruption type 1: replace an argument with wrong_entity
        if rng.random() < 0.5:
            # Replace the first alphanumeric argument
            new_clause = re.sub(r'\b([a-z][a-z0-9_]*)\b', 'wrong_entity_x', clause, count=1)
            corrupted[idx] = new_clause if new_clause != clause else "not_valid_clause_x."
        else:
            # Corruption type 2: negate the predicate
            m = re.match(r'([a-z][a-zA-Z0-9_]*)(.*)', clause.rstrip('.'))
            if m:
                corrupted[idx] = f"not_{m.group(1)}{m.group(2)}."
            else:
                corrupted[idx] = "not_valid_clause_x."
    return corrupted, chosen


def run_calibration_ochiai(
    premises_text: str,
    kb_clauses: list[str],
    prolog,
) -> dict[str, float]:
    """Run oracle queries on KB and return Ochiai scores per predicate."""
    if prolog is None or not kb_clauses:
        return {}
    try:
        oracle_qs = generate_oracle_queries(premises_text, n=N_ORACLE_QUERIES)
        oracle_as = generate_oracle_answers(premises_text, oracle_qs)
    except BudgetExceededError:
        raise
    except Exception:
        return {}

    pass_fail = [a == "yes" for a in oracle_as]
    pred_names = extract_predicate_names_from_clauses(kb_clauses)
    pred_sigs = [f"{n}/{a}" for n, a in pred_names]

    try:
        prolog_goals = nl_queries_to_prolog_batch(oracle_qs, pred_sigs)
    except BudgetExceededError:
        raise
    except Exception:
        return {}

    all_preds_loaded: list[tuple[str, int]] = []
    clear_kb(prolog, all_preds_loaded)
    load_kb_into_prolog(prolog, kb_clauses)
    all_preds_loaded = pred_names

    n_queries = len(prolog_goals)
    coverage_matrix: dict[str, list[str | None]] = {}

    for i, goal in enumerate(prolog_goals):
        if goal == "fail":
            continue
        result = run_oracle_query_with_coverage(prolog, goal)
        for pred_key, outcomes in result["coverage"].items():
            if pred_key not in coverage_matrix:
                coverage_matrix[pred_key] = [None] * n_queries
            worst = outcomes[-1] if outcomes else None
            coverage_matrix[pred_key][i] = worst

    clear_kb(prolog, all_preds_loaded)
    return compute_ochiai_scores(coverage_matrix, pass_fail)


def run_perturbation_calibration(
    examples: list[dict], prolog, n_docs: int = CALIB_N_DOCS
) -> dict[str, Any]:
    """Perturbation-based oracle calibration on first n_docs examples."""
    if prolog is None:
        logger.warning("Prolog not available; skipping perturbation calibration")
        return {"mean_rho": 0.0, "per_doc_rho": [], "valid": False}

    rho_list = []
    for doc_idx in range(min(n_docs, len(examples))):
        ex = examples[doc_idx]
        premises_text = ex.get("metadata_premises_text", "")
        if not premises_text:
            continue
        logger.info(f"Calibration doc {doc_idx}")
        try:
            # 1. Extract KB with dual_sbfl
            clauses = extract_kb(premises_text)
            if len(clauses) < CALIB_N_CORRUPTIONS + 2:
                logger.debug(f"Doc {doc_idx}: KB too small ({len(clauses)}), skipping")
                rho_list.append(0.0)
                continue

            # 2. Inject corruptions
            corrupted_clauses, corrupted_indices = inject_corruptions(
                clauses, n_corrupt=min(CALIB_N_CORRUPTIONS, len(clauses) - 1), seed=doc_idx
            )

            # 3. Get Ochiai scores on corrupted KB
            ochiai_scores = run_calibration_ochiai(premises_text, corrupted_clauses, prolog)

            if not ochiai_scores:
                rho_list.append(0.0)
                continue

            # 4. Build rank vectors
            pred_keys = list(ochiai_scores.keys())
            # Map corrupted clause indices to predicate names
            corrupted_pred_names: set[str] = set()
            for idx in corrupted_indices:
                if idx < len(corrupted_clauses):
                    clause = corrupted_clauses[idx]
                    m = re.match(r'([a-z][a-zA-Z0-9_]*)', clause)
                    if m:
                        corrupted_pred_names.add(m.group(1))

            # Ground-truth rank: corrupted predicates = rank 1, others = rank len(corrupted)+1
            n_corrupt_actual = len(corrupted_pred_names)
            gt_rank = []
            ochiai_rank_scores = []
            for pk in pred_keys:
                pred_name = pk.split("/")[0]
                if pred_name in corrupted_pred_names:
                    gt_rank.append(1)
                else:
                    gt_rank.append(n_corrupt_actual + 1)
                ochiai_rank_scores.append(ochiai_scores[pk])

            if len(set(gt_rank)) < 2:
                # All same rank — can't compute meaningful rho
                rho_list.append(0.0)
                continue

            rho, _ = stats.spearmanr(gt_rank, ochiai_rank_scores)
            rho_list.append(float(rho) if not math.isnan(rho) else 0.0)
            logger.info(f"Calibration doc {doc_idx}: rho={rho:.3f}")

        except BudgetExceededError:
            logger.warning("Budget exceeded during calibration")
            break
        except Exception as e:
            logger.error(f"Calibration doc {doc_idx} failed: {e}")
            rho_list.append(0.0)

    if not rho_list:
        return {"mean_rho": 0.0, "per_doc_rho": [], "valid": False}
    mean_rho = float(np.mean(rho_list))
    logger.info(f"Calibration: mean_rho={mean_rho:.3f} (n={len(rho_list)}), valid={mean_rho >= 0.6}")
    return {"mean_rho": mean_rho, "per_doc_rho": rho_list, "valid": mean_rho >= 0.6}


# ── Figure generation ─────────────────────────────────────────────────────────
COLORS = plt.cm.tab10.colors
METHOD_NAMES = ["dual_sbfl", "one_shot", "cot", "self_refine", "flat_sbfl"]
METHOD_LABELS = ["Dual-SBFL", "One-Shot", "CoT", "Self-Refine", "Flat-SBFL"]


def generate_figures(
    results: list[dict],
    summary: dict,
    calib_result: dict,
    out_dir: Path,
) -> dict[str, str]:
    figs = {}
    n = len(results)
    dataset_label = f"ProofWriter-NL depth-3, N={n}"

    correct_by_method = {m: np.array([1 if r[f"predict_{m}"] == r["gold"] else 0 for r in results])
                         for m in METHOD_NAMES}

    # ── Fig 1: Accuracy per method with 95% CI ────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    accs = {m: float(np.mean(correct_by_method[m])) for m in METHOD_NAMES}
    ci_lo = {}
    ci_hi = {}
    for m in METHOD_NAMES:
        lo, hi = bootstrap_ci_mean(correct_by_method[m])
        ci_lo[m] = accs[m] - lo
        ci_hi[m] = hi - accs[m]
    xs = np.arange(len(METHOD_NAMES))
    bars = ax.bar(xs, [accs[m] for m in METHOD_NAMES],
                  yerr=[[ci_lo[m] for m in METHOD_NAMES], [ci_hi[m] for m in METHOD_NAMES]],
                  capsize=5, color=[COLORS[i] for i in range(len(METHOD_NAMES))],
                  error_kw={"elinewidth": 1.5})
    ax.set_xticks(xs)
    ax.set_xticklabels(METHOD_LABELS, rotation=15, ha='right')
    ax.set_ylabel("Accuracy")
    ax.set_ylim(0, 1.05)
    ax.set_title(f"Multi-Hop Deduction Accuracy by Method\n{dataset_label}")
    for bar, m in zip(bars, METHOD_NAMES):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                f"{accs[m]:.3f}", ha='center', va='bottom', fontsize=9)
    plt.tight_layout()
    path = out_dir / "fig1_accuracy_ci.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig1"] = str(path.name)
    logger.info(f"Saved fig1: {path}")

    # ── Fig 2: Hallucination rate ─────────────────────────────────────────────
    hall_dual = np.array([r.get("hallucination_rate_dual_sbfl", 0.0) for r in results])
    hall_one = np.array([r.get("hallucination_rate_one_shot", 0.0) for r in results])
    fig, ax = plt.subplots(figsize=(8, 5))
    methods_h = ["dual_sbfl", "one_shot"]
    labels_h = ["Dual-SBFL", "One-Shot"]
    hall_arrays = [hall_dual, hall_one]
    hall_means = [float(np.mean(h)) for h in hall_arrays]
    hall_cis = [bootstrap_ci_mean(h) for h in hall_arrays]
    hall_lo = [m - ci[0] for m, ci in zip(hall_means, hall_cis)]
    hall_hi = [ci[1] - m for m, ci in zip(hall_means, hall_cis)]
    xs = np.arange(len(methods_h))
    ax.bar(xs, hall_means, yerr=[hall_lo, hall_hi], capsize=5,
           color=[COLORS[0], COLORS[1]], error_kw={"elinewidth": 1.5})
    ax.set_xticks(xs)
    ax.set_xticklabels(labels_h)
    ax.set_ylabel("Hallucination Rate")
    ax.set_ylim(0, 1.05)
    ax.set_title(f"KB Hallucination Rate (fraction of predicates not in source)\n{dataset_label}")
    for i, (m, v) in enumerate(zip(labels_h, hall_means)):
        ax.text(xs[i], v + 0.01, f"{v:.3f}", ha='center', va='bottom', fontsize=9)
    ci_diff = bootstrap_ci_diff(hall_dual, hall_one)
    ax.text(0.5, 0.92, f"Diff CI [{ci_diff[0]:.3f}, {ci_diff[1]:.3f}]",
            ha='center', transform=ax.transAxes, fontsize=9, style='italic')
    plt.tight_layout()
    path = out_dir / "fig2_hallucination.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig2"] = str(path.name)
    logger.info(f"Saved fig2: {path}")

    # ── Fig 3: LLM calls per document ────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    llm_calls = {m: float(summary["mean_llm_calls"].get(m, 0)) for m in METHOD_NAMES}
    xs = np.arange(len(METHOD_NAMES))
    ax.bar(xs, [llm_calls[m] for m in METHOD_NAMES],
           color=[COLORS[i] for i in range(len(METHOD_NAMES))])
    ax.set_xticks(xs)
    ax.set_xticklabels(METHOD_LABELS, rotation=15, ha='right')
    ax.set_ylabel("Mean LLM API calls per document")
    ax.set_title(f"Efficiency: LLM Calls per Document\n{dataset_label}")
    for i, m in enumerate(METHOD_NAMES):
        ax.text(xs[i], llm_calls[m] + 0.1, f"{llm_calls[m]:.1f}", ha='center', va='bottom', fontsize=9)
    plt.tight_layout()
    path = out_dir / "fig3_efficiency.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig3"] = str(path.name)
    logger.info(f"Saved fig3: {path}")

    # ── Fig 4: Accuracy vs hop depth ─────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    for mi, (m, label) in enumerate(zip(METHOD_NAMES, METHOD_LABELS)):
        depth_accs = {}
        for depth in [1, 2, 3]:
            subset = [r for r in results if r.get("hop_depth", 1) == depth]
            if subset:
                acc = np.mean([1 if r[f"predict_{m}"] == r["gold"] else 0 for r in subset])
                depth_accs[depth] = float(acc)
        if depth_accs:
            depths_sorted = sorted(depth_accs.keys())
            ax.plot(depths_sorted, [depth_accs[d] for d in depths_sorted],
                    marker='o', label=label, color=COLORS[mi])
    ax.set_xlabel("Estimated Hop Depth")
    ax.set_ylabel("Accuracy")
    ax.set_xticks([1, 2, 3])
    ax.set_ylim(0, 1.05)
    ax.legend(loc='best', fontsize=8)
    ax.set_title(f"Accuracy vs Hop Depth by Method\n{dataset_label}")
    plt.tight_layout()
    path = out_dir / "fig4_hop_depth_accuracy.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig4"] = str(path.name)
    logger.info(f"Saved fig4: {path}")

    # ── Fig 5: Calibration rho box plot ──────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    rho_list = calib_result.get("per_doc_rho", [])
    if rho_list:
        ax.boxplot(rho_list, vert=True, patch_artist=True,
                   boxprops=dict(facecolor=COLORS[0], alpha=0.7))
        ax.axhline(y=0.6, color='red', linestyle='--', label='Validity threshold (ρ=0.6)')
        ax.axhline(y=0.0, color='gray', linestyle=':', alpha=0.5)
        ax.set_ylabel("Spearman ρ")
        mean_rho = calib_result.get("mean_rho", 0.0)
        valid_str = "VALID" if calib_result.get("valid", False) else "INVALID"
        ax.set_title(f"Perturbation Calibration: Oracle-Ochiai Ranking Correlation\n"
                     f"N={len(rho_list)} docs | mean ρ={mean_rho:.3f} [{valid_str}]")
        ax.legend()
    else:
        ax.text(0.5, 0.5, "Calibration not available\n(Prolog not available)",
                ha='center', va='center', transform=ax.transAxes)
        ax.set_title("Perturbation Calibration: Oracle-Ochiai Ranking Correlation")
    plt.tight_layout()
    path = out_dir / "fig5_calibration_rho.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig5"] = str(path.name)
    logger.info(f"Saved fig5: {path}")

    # ── Fig 6: KB structural completeness by hop depth ────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    depth_labels = ["1-hop", "2-hop", "3-hop"]
    thresholds = {1: 3, 2: 5, 3: 8}
    success_rates = []
    extraction_fail_rates = []
    repair_fail_rates = []

    for depth in [1, 2, 3]:
        subset = [r for r in results if r.get("hop_depth", 1) == depth]
        if not subset:
            success_rates.append(0.0)
            extraction_fail_rates.append(0.0)
            repair_fail_rates.append(0.0)
            continue
        thresh = thresholds[depth]
        n_sub = len(subset)
        n_success = sum(1 for r in subset if r.get("kb_size_dual", 0) >= thresh)
        n_repair_fail = sum(
            1 for r in subset
            if r.get("kb_size_dual", 0) >= thresh
            and r.get("num_repairs_dual", 0) > 0
            and r.get("predict_dual_sbfl") != r["gold"]
        )
        n_extraction_fail = n_sub - n_success
        success_rates.append(n_success / n_sub)
        extraction_fail_rates.append(n_extraction_fail / n_sub)
        repair_fail_rates.append(n_repair_fail / n_sub)

    xs = np.arange(len(depth_labels))
    ax.bar(xs, extraction_fail_rates, label='Extraction failure', color=COLORS[3], alpha=0.8)
    ax.bar(xs, repair_fail_rates, bottom=extraction_fail_rates, label='Repair failure', color=COLORS[1], alpha=0.8)
    success_bottom = [e + r for e, r in zip(extraction_fail_rates, repair_fail_rates)]
    ax.bar(xs, success_rates, bottom=success_bottom, label='Success', color=COLORS[2], alpha=0.8)
    ax.set_xticks(xs)
    ax.set_xticklabels(depth_labels)
    ax.set_ylabel("Fraction of examples")
    ax.set_ylim(0, 1.1)
    ax.legend(loc='upper right')
    ax.set_title(f"KB Structural Completeness by Hop Depth (Dual-SBFL)\n{dataset_label}")
    plt.tight_layout()
    path = out_dir / "fig6_kb_completeness.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig6"] = str(path.name)
    logger.info(f"Saved fig6: {path}")

    # ── Fig 7: Strict vs Fuzzy Recall CI ──────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    recall_methods = ["dual_sbfl", "one_shot", "self_refine"]
    recall_labels = ["Dual-SBFL", "One-Shot", "Self-Refine"]
    metric_types = ["strict_recall", "fuzzy_recall"]
    metric_labels = ["Strict Recall", "Fuzzy Recall"]

    x = np.arange(len(recall_methods))
    width = 0.35
    for mi, (metric, mlabel) in enumerate(zip(metric_types, metric_labels)):
        means = []
        ci_lo_list = []
        ci_hi_list = []
        for m in recall_methods:
            vals = np.array([r.get(f"kb_{metric}_{m}", 0.0) for r in results])
            mean_val = float(np.mean(vals))
            lo, hi = bootstrap_ci_mean(vals)
            means.append(mean_val)
            ci_lo_list.append(mean_val - lo)
            ci_hi_list.append(hi - mean_val)
        offset = (mi - 0.5) * width
        ax.bar(x + offset, means, width=width,
               yerr=[ci_lo_list, ci_hi_list], capsize=4,
               color=COLORS[mi], alpha=0.8, label=mlabel, error_kw={"elinewidth": 1.5})
    ax.set_xticks(x)
    ax.set_xticklabels(recall_labels)
    ax.set_ylabel("Recall")
    ax.set_ylim(0, 1.1)
    ax.legend()
    ax.set_title(f"Strict vs Fuzzy KB Recall with 95% Bootstrap CI\n{dataset_label}")
    plt.tight_layout()
    path = out_dir / "fig7_strict_vs_fuzzy_recall_ci.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    figs["fig7"] = str(path.name)
    logger.info(f"Saved fig7: {path}")

    return figs


# ── Checkpoint ────────────────────────────────────────────────────────────────
CHECKPOINT_PATH = WORKSPACE / "checkpoint.json"


def save_checkpoint(results: list[dict]) -> None:
    CHECKPOINT_PATH.write_text(json.dumps(results))


def load_checkpoint() -> list[dict]:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text())
        except Exception:
            pass
    return []


# ── Main ──────────────────────────────────────────────────────────────────────
@logger.catch(reraise=True)
def main() -> None:
    logger.info("=" * 60)
    logger.info("Dual-Signal SBFL Rigorous Evaluation on ProofWriter-NL depth-3")
    logger.info("=" * 60)

    # Load ProofWriter examples
    data_path = Path("/ai-inventor/aii_data/runs/run_ITc1Qruy7fap/3_invention_loop/iter_1/gen_art/gen_art_dataset_1/full_data_out.json")
    logger.info(f"Loading dataset from {data_path}")

    raw = json.loads(data_path.read_text())
    all_datasets = raw.get("datasets", [])
    proofwriter_ds = next(
        (ds for ds in all_datasets if ds["dataset"] == "proofwriter_ruletaker_depth3"), None
    )
    if proofwriter_ds is None:
        raise ValueError("proofwriter_ruletaker_depth3 dataset not found")

    all_examples = proofwriter_ds["examples"]
    logger.info(f"Total ProofWriter examples: {len(all_examples)}")
    examples = all_examples[:N_EXAMPLES]
    logger.info(f"Using {len(examples)} examples")

    # Init Prolog
    prolog = get_prolog()
    logger.info(f"Prolog available: {PROLOG_AVAILABLE}")

    # Load checkpoint if exists
    completed_results = load_checkpoint()
    start_idx = len(completed_results)
    logger.info(f"Resuming from checkpoint at example {start_idx}")

    # Track LLM calls per method
    llm_calls_per_method: dict[str, list[int]] = {m: [] for m in METHOD_NAMES}

    # Main evaluation loop
    all_results = list(completed_results)

    for i, ex in enumerate(examples[start_idx:], start=start_idx):
        if COST_TRACKER["total"] >= MAX_BUDGET_USD:
            logger.warning(f"Budget exceeded at example {i}. Stopping.")
            break

        premises_text = ex.get("metadata_premises_text", "")
        conclusion_nl = ex.get("metadata_conclusion", "")
        gold_label = ex.get("output", "").strip()
        example_id = ex.get("metadata_example_id", str(i))

        # Normalize gold: ProofWriter uses True/False
        if gold_label.lower() == "true":
            gold_label = "True"
        elif gold_label.lower() == "false":
            gold_label = "False"

        hop_depth = estimate_hop_depth(premises_text)

        result: dict[str, Any] = {
            "input": ex.get("input", ""),
            "output": gold_label,
            "gold": gold_label,
            "hop_depth": hop_depth,
            "metadata_example_id": example_id,
            "metadata_premises_text": premises_text[:300],
            "metadata_conclusion": conclusion_nl,
        }

        calls_before = COST_TRACKER["calls"]
        dual_precomputed = None

        # 1. Dual-SBFL
        try:
            prolog = get_prolog()
            calls_before_dual = COST_TRACKER["calls"]
            r = run_dual_sbfl_proofwriter(premises_text, conclusion_nl, prolog, flat_mode=False)
            result["predict_dual_sbfl"] = r["predicted_label"]
            result["num_repairs_dual"] = r["num_repairs"]
            result["kb_size_dual"] = r["kb_size"]
            result["metadata_ochiai_top5"] = json.dumps(r["ochiai_top5"])
            result["metadata_subgoal_top5"] = json.dumps(r["subgoal_top5"])
            dual_clauses = r["kb_clauses"]
            dual_precomputed = r.get("precomputed")
            llm_calls_per_method["dual_sbfl"].append(COST_TRACKER["calls"] - calls_before_dual)
        except BudgetExceededError:
            logger.warning(f"Budget exceeded at example {i}")
            break
        except Exception as e:
            logger.error(f"dual_sbfl failed on ex {i}: {e}")
            result["predict_dual_sbfl"] = "Uncertain"
            result["num_repairs_dual"] = 0
            result["kb_size_dual"] = 0
            result["metadata_ochiai_top5"] = "[]"
            result["metadata_subgoal_top5"] = "[]"
            dual_clauses = []
            reset_prolog()

        # Compute hallucination and recall for dual_sbfl
        initial_clauses = dual_precomputed.get("clauses", []) if dual_precomputed else dual_clauses
        if initial_clauses:
            result["hallucination_rate_dual_sbfl"] = compute_hallucination_rate(initial_clauses, premises_text)
            pr = compute_kb_precision_recall(initial_clauses, premises_text)
            result["kb_strict_recall_dual_sbfl"] = pr["strict_recall"]
            result["kb_fuzzy_recall_dual_sbfl"] = pr["fuzzy_recall"]
            result["kb_strict_precision_dual_sbfl"] = pr["strict_precision"]
            result["kb_fuzzy_precision_dual_sbfl"] = pr["fuzzy_precision"]
        else:
            result["hallucination_rate_dual_sbfl"] = 0.0
            result["kb_strict_recall_dual_sbfl"] = 0.0
            result["kb_fuzzy_recall_dual_sbfl"] = 0.0
            result["kb_strict_precision_dual_sbfl"] = 0.0
            result["kb_fuzzy_precision_dual_sbfl"] = 0.0

        # 2. One-shot — reuse KB from precomputed (no extra extraction)
        try:
            prolog = get_prolog()
            calls_before_one = COST_TRACKER["calls"]
            one_clauses = dual_precomputed.get("clauses", []) if dual_precomputed else []
            if one_clauses and not PROLOG_AVAILABLE:
                oneshot_pred = evaluate_conclusion_llm(conclusion_nl, one_clauses)
            else:
                oneshot_pred = run_oneshot_proofwriter(premises_text, conclusion_nl, prolog)
            result["predict_one_shot"] = oneshot_pred
            llm_calls_per_method["one_shot"].append(COST_TRACKER["calls"] - calls_before_one)
        except BudgetExceededError:
            break
        except Exception as e:
            logger.error(f"one_shot failed on ex {i}: {e}")
            result["predict_one_shot"] = "Uncertain"
            reset_prolog()
            one_clauses = initial_clauses

        # Hallucination for one_shot: use same initial KB (no re-extraction)
        h_clauses = one_clauses if one_clauses else initial_clauses
        result["hallucination_rate_one_shot"] = compute_hallucination_rate(h_clauses, premises_text)
        pr_one = compute_kb_precision_recall(h_clauses, premises_text)
        result["kb_strict_recall_one_shot"] = pr_one["strict_recall"]
        result["kb_fuzzy_recall_one_shot"] = pr_one["fuzzy_recall"]

        # 3. CoT
        try:
            calls_before_cot = COST_TRACKER["calls"]
            cot_pred = run_cot_proofwriter(premises_text, conclusion_nl)
            result["predict_cot"] = cot_pred
            llm_calls_per_method["cot"].append(COST_TRACKER["calls"] - calls_before_cot)
        except BudgetExceededError:
            break
        except Exception as e:
            logger.error(f"cot failed on ex {i}: {e}")
            result["predict_cot"] = "Uncertain"

        # 4. Self-refine
        try:
            prolog = get_prolog()
            calls_before_sr = COST_TRACKER["calls"]
            sr_pred = run_selfrefine_proofwriter(premises_text, conclusion_nl, prolog)
            result["predict_self_refine"] = sr_pred
            # Reuse dual_sbfl KB for recall approx (no extra extraction)
            pr_sr = compute_kb_precision_recall(initial_clauses, premises_text)
            result["kb_strict_recall_self_refine"] = pr_sr["strict_recall"]
            result["kb_fuzzy_recall_self_refine"] = pr_sr["fuzzy_recall"]
            llm_calls_per_method["self_refine"].append(COST_TRACKER["calls"] - calls_before_sr)
        except BudgetExceededError:
            break
        except Exception as e:
            logger.error(f"self_refine failed on ex {i}: {e}")
            result["predict_self_refine"] = "Uncertain"
            result["kb_strict_recall_self_refine"] = 0.0
            result["kb_fuzzy_recall_self_refine"] = 0.0
            reset_prolog()

        # 5. Flat SBFL — reuse dual_sbfl precomputed KB/oracle (saves ~4 LLM calls)
        try:
            prolog = get_prolog()
            calls_before_flat = COST_TRACKER["calls"]
            r_flat = run_dual_sbfl_proofwriter(
                premises_text, conclusion_nl, prolog, flat_mode=True,
                precomputed=dual_precomputed,
            )
            result["predict_flat_sbfl"] = r_flat["predicted_label"]
            llm_calls_per_method["flat_sbfl"].append(COST_TRACKER["calls"] - calls_before_flat)
        except BudgetExceededError:
            break
        except Exception as e:
            logger.error(f"flat_sbfl failed on ex {i}: {e}")
            result["predict_flat_sbfl"] = "Uncertain"
            reset_prolog()

        # Track total LLM calls per example
        result["metadata_llm_calls_total"] = COST_TRACKER["calls"] - calls_before
        result["metadata_cumulative_cost_usd"] = f"{COST_TRACKER['total']:.4f}"

        all_results.append(result)

        # Checkpoint every 10 examples
        if (i + 1) % 10 == 0:
            save_checkpoint(all_results)
            n_done = len(all_results)
            accs = {m: np.mean([1 if r.get(f"predict_{m}") == r["gold"] else 0 for r in all_results])
                    for m in METHOD_NAMES}
            logger.info(
                f"[{i+1}/{len(examples)}] n={n_done} | "
                f"dual={accs['dual_sbfl']:.3f} one_shot={accs['one_shot']:.3f} "
                f"cot={accs['cot']:.3f} cost=${COST_TRACKER['total']:.2f}"
            )

        gc.collect()

    n_done = len(all_results)
    logger.info(f"Evaluation complete: n={n_done} examples processed, cost=${COST_TRACKER['total']:.4f}")

    if n_done == 0:
        logger.error("No examples processed!")
        return

    # ── Compute metrics ────────────────────────────────────────────────────────
    logger.info("Computing metrics...")

    # Accuracy arrays
    correct_arrays = {
        m: np.array([1 if r.get(f"predict_{m}") == r["gold"] else 0 for r in all_results])
        for m in METHOD_NAMES
    }
    accuracy_by_method = {m: float(np.mean(correct_arrays[m])) for m in METHOD_NAMES}

    # Bootstrap CIs for pairwise differences
    pairs = [
        ("dual_sbfl", "one_shot"),
        ("dual_sbfl", "self_refine"),
        ("dual_sbfl", "flat_sbfl"),
        ("dual_sbfl", "cot"),
    ]
    bootstrap_cis = {}
    for m1, m2 in pairs:
        lo, hi = bootstrap_ci_diff(correct_arrays[m1], correct_arrays[m2])
        bootstrap_cis[f"{m1}_vs_{m2}"] = {
            "ci_lo": lo, "ci_hi": hi,
            "excludes_zero": lo > 0 or hi < 0,
        }

    # Cohen's h
    cohens_h_vals = {}
    for m1, m2 in pairs:
        cohens_h_vals[f"{m1}_vs_{m2}"] = cohens_h(accuracy_by_method[m1], accuracy_by_method[m2])

    # Hallucination rates
    hall_dual = np.array([r.get("hallucination_rate_dual_sbfl", 0.0) for r in all_results])
    hall_one = np.array([r.get("hallucination_rate_one_shot", 0.0) for r in all_results])
    hall_ci = bootstrap_ci_diff(hall_dual, hall_one)

    # Precision / recall
    pr_metrics = {}
    for metric in ["strict_recall", "fuzzy_recall", "strict_precision", "fuzzy_precision"]:
        for m in ["dual_sbfl", "one_shot", "self_refine"]:
            vals = np.array([r.get(f"kb_{metric}_{m}", 0.0) for r in all_results])
            pr_metrics[f"{m}_{metric}"] = float(np.mean(vals))

    # Bootstrap CIs for recall
    recall_cis = {}
    for metric in ["strict_recall", "fuzzy_recall"]:
        for m2 in ["one_shot", "self_refine"]:
            a = np.array([r.get(f"kb_{metric}_dual_sbfl", 0.0) for r in all_results])
            b = np.array([r.get(f"kb_{metric}_{m2}", 0.0) for r in all_results])
            lo, hi = bootstrap_ci_diff(a, b)
            recall_cis[f"dual_sbfl_vs_{m2}_{metric}"] = {"ci_lo": lo, "ci_hi": hi}

    # Mean LLM calls per method
    mean_llm_calls = {m: float(np.mean(v)) if v else 0.0 for m, v in llm_calls_per_method.items()}

    # Hop depth accuracy
    hop_depth_accuracy: dict[str, dict[str, float]] = {}
    for depth in [1, 2, 3]:
        subset = [r for r in all_results if r.get("hop_depth", 1) == depth]
        if subset:
            hop_depth_accuracy[str(depth)] = {
                m: float(np.mean([1 if r.get(f"predict_{m}") == r["gold"] else 0 for r in subset]))
                for m in METHOD_NAMES
            }
            hop_depth_accuracy[str(depth)]["n"] = len(subset)

    # KB completeness by hop depth
    kb_completeness: dict[str, dict] = {}
    thresholds = {1: 3, 2: 5, 3: 8}
    for depth in [1, 2, 3]:
        subset = [r for r in all_results if r.get("hop_depth", 1) == depth]
        if not subset:
            kb_completeness[str(depth)] = {"n": 0}
            continue
        thresh = thresholds[depth]
        n_sub = len(subset)
        n_complete = sum(1 for r in subset if r.get("kb_size_dual", 0) >= thresh)
        n_repaired = sum(1 for r in subset if r.get("num_repairs_dual", 0) > 0)
        kb_completeness[str(depth)] = {
            "n": n_sub,
            "completeness_rate": n_complete / n_sub,
            "repair_rate": n_repaired / n_sub,
        }

    # ── Perturbation calibration ───────────────────────────────────────────────
    logger.info("Running perturbation calibration...")
    calib_result = run_perturbation_calibration(examples, get_prolog(), n_docs=CALIB_N_DOCS)

    # ── Generate figures ───────────────────────────────────────────────────────
    summary_for_figs = {"mean_llm_calls": mean_llm_calls}
    logger.info("Generating figures...")
    figs = generate_figures(all_results, summary_for_figs, calib_result, WORKSPACE)

    # ── Build metrics_agg (schema-required flat dict of floats) ───────────────
    metrics_agg: dict[str, float] = {}
    for m in METHOD_NAMES:
        metrics_agg[f"accuracy_{m}"] = accuracy_by_method[m]
    for pair_key, vals in bootstrap_cis.items():
        metrics_agg[f"ci_lo_{pair_key}"] = vals["ci_lo"]
        metrics_agg[f"ci_hi_{pair_key}"] = vals["ci_hi"]
    for pair_key, h in cohens_h_vals.items():
        metrics_agg[f"cohens_h_{pair_key}"] = h
    metrics_agg["mean_hallucination_dual_sbfl"] = float(np.mean(hall_dual))
    metrics_agg["mean_hallucination_one_shot"] = float(np.mean(hall_one))
    metrics_agg["hallucination_diff_ci_lo"] = hall_ci[0]
    metrics_agg["hallucination_diff_ci_hi"] = hall_ci[1]
    for k, v in pr_metrics.items():
        metrics_agg[f"mean_{k}"] = v
    metrics_agg["calibration_rho_mean"] = calib_result["mean_rho"]
    metrics_agg["calibration_valid"] = 1.0 if calib_result["valid"] else 0.0
    for m in METHOD_NAMES:
        metrics_agg[f"mean_llm_calls_{m}"] = mean_llm_calls[m]
    metrics_agg["n_examples_processed"] = float(n_done)
    metrics_agg["total_cost_usd"] = COST_TRACKER["total"]

    # ── Build per-example output (schema-compliant) ───────────────────────────
    out_examples = []
    for r in all_results:
        ex_out: dict[str, Any] = {
            "input": r.get("input", ""),
            "output": r.get("output", ""),
            "predict_dual_sbfl": r.get("predict_dual_sbfl", "Uncertain"),
            "predict_one_shot": r.get("predict_one_shot", "Uncertain"),
            "predict_cot": r.get("predict_cot", "Uncertain"),
            "predict_self_refine": r.get("predict_self_refine", "Uncertain"),
            "predict_flat_sbfl": r.get("predict_flat_sbfl", "Uncertain"),
            "eval_correct_dual_sbfl": 1.0 if r.get("predict_dual_sbfl") == r.get("gold") else 0.0,
            "eval_correct_one_shot": 1.0 if r.get("predict_one_shot") == r.get("gold") else 0.0,
            "eval_correct_cot": 1.0 if r.get("predict_cot") == r.get("gold") else 0.0,
            "eval_correct_self_refine": 1.0 if r.get("predict_self_refine") == r.get("gold") else 0.0,
            "eval_correct_flat_sbfl": 1.0 if r.get("predict_flat_sbfl") == r.get("gold") else 0.0,
            "eval_hallucination_dual_sbfl": r.get("hallucination_rate_dual_sbfl", 0.0),
            "eval_hallucination_one_shot": r.get("hallucination_rate_one_shot", 0.0),
            "eval_hop_depth": float(r.get("hop_depth", 1)),
            "eval_kb_size_dual": float(r.get("kb_size_dual", 0)),
            "eval_num_repairs_dual": float(r.get("num_repairs_dual", 0)),
            "eval_strict_recall_dual_sbfl": r.get("kb_strict_recall_dual_sbfl", 0.0),
            "eval_fuzzy_recall_dual_sbfl": r.get("kb_fuzzy_recall_dual_sbfl", 0.0),
            "eval_strict_recall_one_shot": r.get("kb_strict_recall_one_shot", 0.0),
            "eval_fuzzy_recall_one_shot": r.get("kb_fuzzy_recall_one_shot", 0.0),
            "metadata_example_id": r.get("metadata_example_id", ""),
            "metadata_conclusion": r.get("metadata_conclusion", ""),
            "metadata_ochiai_top5": r.get("metadata_ochiai_top5", "[]"),
            "metadata_subgoal_top5": r.get("metadata_subgoal_top5", "[]"),
            "metadata_cumulative_cost_usd": r.get("metadata_cumulative_cost_usd", "0.0"),
            "metadata_llm_calls": str(r.get("metadata_llm_calls_total", 0)),
        }
        out_examples.append(ex_out)

    # ── Build full eval_out.json ───────────────────────────────────────────────
    eval_out = {
        "metadata": {
            "evaluation_name": "dual_sbfl_rigorous_eval_iter2",
            "main_dataset": "proofwriter_ruletaker_depth3",
            "n_examples": n_done,
            "folio_note": "FOLIO evaluation was preliminary (N=3, incomplete run); excluded from main results. Main evaluation is ProofWriter-NL depth-3.",
            "bootstrap_n_resamples": N_BOOTSTRAP,
            "calibration_n_docs": CALIB_N_DOCS,
            "model": MODEL,
            "dataset_disambiguation": {
                "folio": "PRELIMINARY ONLY — 3 examples, incomplete run, NOT included in main metrics",
                "proofwriter_ruletaker_depth3": f"MAIN EVALUATION — N={n_done} examples, all results reported here",
            },
            "accuracy_by_method": accuracy_by_method,
            "bootstrap_cis_pairwise": bootstrap_cis,
            "cohens_h": cohens_h_vals,
            "hallucination_rates": {
                "dual_sbfl_mean": float(np.mean(hall_dual)),
                "one_shot_mean": float(np.mean(hall_one)),
                "diff_ci_95": list(hall_ci),
            },
            "precision_recall": pr_metrics,
            "recall_bootstrap_cis": recall_cis,
            "mean_llm_calls_per_doc": mean_llm_calls,
            "calibration": {
                "mean_rho": calib_result["mean_rho"],
                "per_doc_rho": calib_result["per_doc_rho"],
                "validity": "VALID" if calib_result["valid"] else "INVALID",
                "threshold": 0.6,
                "note": "N=10 docs; no p-values reported (n=10 makes t-test p-values meaningless)",
            },
            "hop_depth_accuracy": hop_depth_accuracy,
            "kb_completeness_by_depth": kb_completeness,
            "figures": figs,
            "total_cost_usd": COST_TRACKER["total"],
            "total_llm_calls": COST_TRACKER["calls"],
        },
        "metrics_agg": metrics_agg,
        "datasets": [
            {
                "dataset": "proofwriter_ruletaker_depth3",
                "examples": out_examples,
            }
        ],
    }

    out_path = WORKSPACE / "eval_out.json"
    out_path.write_text(json.dumps(eval_out, indent=2))
    logger.info(f"Saved eval_out.json ({out_path.stat().st_size / 1024:.1f} KB)")

    # ── Summary log ───────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("EVALUATION SUMMARY")
    logger.info(f"  Dataset: ProofWriter-NL depth-3, N={n_done}")
    logger.info(f"  FOLIO: EXCLUDED (preliminary N=3 run only)")
    for m in METHOD_NAMES:
        logger.info(f"  {m} accuracy: {accuracy_by_method[m]:.4f}")
    logger.info(f"  Bootstrap CI (dual_sbfl vs one_shot): {bootstrap_cis.get('dual_sbfl_vs_one_shot', {})}")
    logger.info(f"  Hallucination: dual={float(np.mean(hall_dual)):.3f}, one_shot={float(np.mean(hall_one)):.3f}")
    logger.info(f"  Calibration rho: {calib_result['mean_rho']:.3f} ({'VALID' if calib_result['valid'] else 'INVALID'})")
    logger.info(f"  Cost: ${COST_TRACKER['total']:.4f}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
