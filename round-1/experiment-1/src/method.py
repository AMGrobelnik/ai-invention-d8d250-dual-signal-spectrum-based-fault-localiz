#!/usr/bin/env python3
"""Dual-Signal SBFL Pipeline for NL-to-FOL Prolog KB Repair on FOLIO.

Methods:
  - dual_sbfl: Ochiai SBFL + sub-goal harvesting (our method)
  - oneshot: extract KB, query conclusion directly
  - cot: chain-of-thought LLM reasoning
  - selfrefine: iterative KB refinement with oracle feedback
  - flat_sbfl: binary-coverage SBFL (ablation, no stratification/sub-goal harvest)
"""

import gc
import json
import math
import os
import re
import resource
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from datasets import load_dataset
from loguru import logger
from tqdm import tqdm

# ── Logging ──────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

WORKSPACE = Path(__file__).parent

# ── Hardware / memory limits ─────────────────────────────────────────────────
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
RAM_BUDGET = int(TOTAL_RAM_GB * 0.7 * 1e9)
resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET * 3, RAM_BUDGET * 3))
logger.info(f"Container RAM: {TOTAL_RAM_GB:.1f}GB, budget: {RAM_BUDGET/1e9:.1f}GB")

# ── Configuration ─────────────────────────────────────────────────────────────
MODEL = "meta-llama/llama-3.1-8b-instruct"
FALLBACK_MODEL = "google/gemma-2-9b-it"
MAX_BUDGET_USD = 9.5
MAX_EXAMPLES = 203        # use full validation set (203 examples)
N_ORACLE_QUERIES = 5      # LLM generates this many yes/no queries  (note: batch converted, so 1 LLM call)
N_REPAIR_ROUNDS = 1
K_REPAIR_TARGETS = 3
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

COST_TRACKER: dict[str, float] = {"total": 0.0, "calls": 0}

# ── LLM helper ────────────────────────────────────────────────────────────────
# Approximate pricing per million tokens (OpenRouter, as of 2025)
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "meta-llama/llama-3.1-8b-instruct": (0.055, 0.055),
    "google/gemma-2-9b-it": (0.07, 0.07),
    "anthropic/claude-3-haiku": (0.25, 1.25),
}


class BudgetExceededError(RuntimeError):
    pass


def llm_call(
    messages: list[dict],
    model: str = MODEL,
    max_tokens: int = 512,
    temperature: float = 0.0,
    retries: int = 3,
) -> tuple[str, float]:
    """Call OpenRouter API. Returns (text, cost_usd)."""
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
                headers=headers,
                json=payload,
                timeout=120,
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
                f"LLM call #{COST_TRACKER['calls']} model={model} "
                f"in={in_tok} out={out_tok} cost=${cost:.5f} total=${COST_TRACKER['total']:.3f}"
            )
            return text, cost
        except BudgetExceededError:
            raise
        except Exception as e:
            last_err = e
            logger.warning(f"LLM attempt {attempt+1} failed: {e}")
            time.sleep(2 * (attempt + 1))

    logger.error(f"All LLM retries failed: {last_err}")
    return "", 0.0


# ── Prolog meta-interpreter ───────────────────────────────────────────────────
META_INTERPRETER_PROLOG = """\
:- dynamic coverage_log/3.
:- dynamic failed_subgoal/1.

reset_coverage :-
    retractall(coverage_log(_, _, _)),
    retractall(failed_subgoal(_)).

% Entry point with depth limit to prevent stack overflow from cyclic KBs
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


def _ensure_meta_interp_file() -> str:
    global _META_INTERP_FILE
    if _META_INTERP_FILE and Path(_META_INTERP_FILE).exists():
        return _META_INTERP_FILE
    tf = tempfile.NamedTemporaryFile(
        suffix=".pl", mode="w", delete=False, dir="/tmp"
    )
    tf.write(META_INTERPRETER_PROLOG)
    tf.flush()
    tf.close()
    _META_INTERP_FILE = tf.name
    return _META_INTERP_FILE


# ── Prolog instance management ────────────────────────────────────────────────
_prolog_instance = None


def get_prolog(force_new: bool = False):
    global _prolog_instance
    if _prolog_instance is None or force_new:
        from pyswip import Prolog  # type: ignore

        p = Prolog()
        # Increase stack limit to 256MB to handle cyclic KBs
        try:
            list(p.query("set_prolog_flag(stack_limit, 268435456)"))  # 256MB
        except Exception:
            pass
        mf = _ensure_meta_interp_file()
        try:
            list(p.query(f'consult("{mf}")'))
        except Exception as e:
            logger.warning(f"Meta-interp load warning: {e}")
        _prolog_instance = p
        logger.info("Prolog instance initialized with meta-interpreter")
    return _prolog_instance


def reset_prolog() -> None:
    """Reset the Prolog instance if it appears crashed."""
    global _prolog_instance
    _prolog_instance = None
    gc.collect()
    logger.info("Prolog instance reset")


_PREDICATE_SIGNATURE_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*/\d+$")
_PROLOG_DANGEROUS_RE = re.compile(r"\b(assert|retract|abolish|consult|halt|write|read|open|close|nl)\b")


def sanitize_clause(clause: str) -> str | None:
    """Return a safe, valid-looking Prolog clause or None to discard."""
    c = clause.strip().rstrip(".")
    if not c:
        return None
    # Discard predicate-signature lines like  foo/1
    if _PREDICATE_SIGNATURE_RE.match(c):
        return None
    # Discard meta/dangerous predicates
    if _PROLOG_DANGEROUS_RE.search(c):
        return None
    # Remove any comment suffix  (% ...)
    c = re.sub(r"\s*%.*$", "", c).strip()
    # Replace 'not(X)' → '\+(X)' and 'not(X,...)' patterns
    c = re.sub(r"\bnot\s*\(", r"\\+(", c)
    # Discard any clause with remaining bare 'not ' (LLM used invalid syntax)
    if re.search(r"\bnot\s+[a-zA-Z_]", c):
        return None
    # Discard facts that are negations - \+(foo) can't be asserted as a fact
    if c.lstrip().startswith("\\+"):
        return None
    # Discard clauses with conjunctions in the head (top-level comma before :-)
    head_part = c.split(":-")[0].strip()
    depth = 0
    for ch in head_part:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            return None  # conjunction in head is invalid Prolog
    # Discard clauses with '!' in rule body if it looks malformed
    # Discard lines that look like comments slipped through
    if c.startswith("/*") or c.startswith("*/"):
        return None
    # Discard numbered-list items or markdown code that slipped through
    if re.match(r"^\d+\.", c) or "`" in c or c.startswith("-") or c.startswith("*"):
        return None
    # Discard lines starting with uppercase (not valid Prolog clause heads)
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
            # count top-level commas
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
        # Re-sanitize just in case
        safe = sanitize_clause(clause)
        if safe is None:
            continue
        c = safe.rstrip(".")
        try:
            prolog.assertz(c)
            ok.append(safe)
        except Exception as e:
            logger.debug(f"assertz failed for '{safe[:60]}': {e}")
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
    """Run a Prolog query wrapped in SWI-Prolog's native call_with_time_limit/2.

    call_with_time_limit uses SIGALRM internally and works inside C extensions.
    """
    # Wrap with SWI-Prolog's native time limit
    wrapped = f"catch(call_with_time_limit({timeout_sec}, ({query_str})), time_limit_exceeded, fail)"
    try:
        return list(prolog.query(wrapped))
    except Exception as e:
        logger.debug(f"Prolog query error for '{query_str[:60]}': {e}")
        return []


def run_oracle_query_with_coverage(
    prolog, goal_str: str, depth: int = 2
) -> dict[str, Any]:
    try:
        _prolog_query_with_timeout(prolog, "reset_coverage", timeout_sec=3)
    except Exception:
        pass

    succeeded = False
    try:
        # Use solve_safe which wraps with call_with_depth_limit
        results = _prolog_query_with_timeout(
            prolog, f"solve_safe(({goal_str}), {depth})", timeout_sec=5
        )
        succeeded = len(results) > 0
    except Exception as e:
        logger.debug(f"meta-interp query failed for '{goal_str[:60]}': {e}")

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

    return {
        "succeeded": succeeded,
        "coverage": coverage,
        "failed_subgoals": failed_subgoals,
    }


# ── Extraction prompts ────────────────────────────────────────────────────────
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
half 'no' or 'unknown'. Format: one question per line, starting with 'Q: '.

Premises:
{premises}

Questions:"""

ANSWER_PROMPT = """\
Based on the premises, answer each question with 'yes', 'no', or 'unknown'. \
Output ONLY 'yes', 'no', or 'unknown' for each question, one answer per line.

Premises:
{premises}

Questions:
{questions}

Answers (one per line):"""

NL_TO_PROLOG_PROMPT = """\
Given this Prolog KB and a yes/no question, write a single Prolog goal (without '?-') \
that answers the question. Use predicates from the KB. Output ONLY the Prolog goal.
If the question cannot be answered from this KB, output: fail

KB predicates: {predicates}

Question: {question}
Prolog goal:"""

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

CONCLUSION_EVAL_PROMPT = """\
Convert this FOL conclusion to a Prolog goal (without '?-').
Use ONLY predicates listed below. Output exactly one line: the Prolog goal expression.
No explanations, no punctuation except the goal itself.
If it cannot be converted, output exactly: fail

Conclusion: {conclusion_fol}

Available KB predicates: {predicates}

Prolog goal (single line only):"""


# ── LLM pipeline functions ────────────────────────────────────────────────────
def _parse_clauses_from_text(text: str) -> list[str]:
    """Extract and sanitize Prolog clauses from LLM response text."""
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
        [{"role": "user", "content": EXTRACTION_PROMPT.format(premises=premises_text)}],
        max_tokens=800,
    )
    return _parse_clauses_from_text(response)


def generate_oracle_queries(premises_text: str, n: int = N_ORACLE_QUERIES) -> list[str]:
    response, _ = llm_call(
        [{"role": "user", "content": ORACLE_PROMPT.format(premises=premises_text, n=n)}],
        max_tokens=400,
    )
    lines = [
        ln.strip().lstrip("Q:").lstrip("q:").strip()
        for ln in response.split("\n")
        if ln.strip().upper().startswith("Q:")
    ]
    return lines[:n]


def generate_oracle_answers(
    premises_text: str, questions: list[str]
) -> list[str]:
    q_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    response, _ = llm_call(
        [{"role": "user", "content": ANSWER_PROMPT.format(
            premises=premises_text, questions=q_block
        )}],
        max_tokens=200,
    )
    raw_lines = [ln.strip().lower() for ln in response.split("\n") if ln.strip()]
    answers: list[str] = []
    for ln in raw_lines:
        # extract first valid keyword
        for word in re.split(r"[^a-z]+", ln):
            if word in ("yes", "no", "unknown"):
                answers.append(word)
                break
        if len(answers) == len(questions):
            break
    while len(answers) < len(questions):
        answers.append("unknown")
    return answers[: len(questions)]


def sanitize_prolog_goal(goal: str) -> str:
    """Sanitize an LLM-generated Prolog goal for use in a query."""
    if not goal:
        return "fail"
    # Replace 'not(X)' → '\+(X)'
    goal = re.sub(r"\bnot\s*\(", r"\\+(", goal)
    # Replace 'not X' (bare) → 'fail' (safer than trying to fix)
    if re.search(r"\bnot\s+[a-zA-Z_]", goal):
        return "fail"
    # Reject goals with invalid Prolog characters: ~, !, @, #, $
    if re.search(r"[~@#$]", goal):
        return "fail"
    # Reject goals with dangerous built-ins
    dangerous = ["assert", "retract", "abolish", "consult", "halt", "write", "nl"]
    if any(d in goal.lower() for d in dangerous):
        return "fail"
    # Lowercase leading CamelCase predicates
    goal = re.sub(r'\b([A-Z][a-zA-Z0-9_]*)\s*\(', lambda m: m.group(1).lower() + '(', goal)
    # Reject goals that start with invalid characters
    if re.match(r'^[^a-z\(\\]', goal.strip()):
        return "fail"
    return goal


def nl_query_to_prolog(question: str, pred_sigs: list[str]) -> str:
    sig_block = ", ".join(pred_sigs[:30])
    response, _ = llm_call(
        [{"role": "user", "content": NL_TO_PROLOG_PROMPT.format(
            predicates=sig_block, question=question
        )}],
        max_tokens=100,
        temperature=0.0,
    )
    goal = response.strip().rstrip(".").strip()
    return sanitize_prolog_goal(goal)


BATCH_NL_TO_PROLOG_PROMPT = """\
Convert each yes/no question to a Prolog goal (without '?-').
Use ONLY predicates listed. Output one goal per line (no numbering, no explanation).
If a question cannot be converted, output: fail

Available KB predicates: {predicates}

Questions:
{questions}

Prolog goals (one per line):"""


def nl_queries_to_prolog_batch(questions: list[str], pred_sigs: list[str]) -> list[str]:
    """Convert multiple NL questions to Prolog goals in a single LLM call."""
    if not questions:
        return []
    sig_block = ", ".join(pred_sigs[:30])
    q_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    response, _ = llm_call(
        [{"role": "user", "content": BATCH_NL_TO_PROLOG_PROMPT.format(
            predicates=sig_block, questions=q_block
        )}],
        max_tokens=300,
        temperature=0.0,
    )
    lines = [ln.strip().rstrip(".") for ln in response.split("\n") if ln.strip()]
    # Filter and sanitize
    goals = []
    for ln in lines:
        # Skip numbered lines like "1." or lines that are questions
        if re.match(r"^\d+\.", ln):
            parts = ln.split(".", 1)
            if len(parts) > 1:
                ln = parts[1].strip()
        if ln and not ln.startswith("#") and not ln.startswith("//"):
            goals.append(sanitize_prolog_goal(ln))
    # Pad or truncate to match number of questions
    while len(goals) < len(questions):
        goals.append("fail")
    return goals[: len(questions)]


def evaluate_conclusion(
    prolog, conclusion_fol: str, kb_clauses: list[str], pred_sigs: list[str]
) -> str:
    goal = conclusion_fol.strip().rstrip(".")
    needs_llm = any(
        tok in goal
        for tok in ["∀", "∃", "→", "⟹", "=>", "forall", "exists", "⊕", "¬", "∧", "∨"]
    )
    if needs_llm or len(goal) > 80:
        sig_block = ", ".join(pred_sigs[:30])
        response, _ = llm_call(
            [{"role": "user", "content": CONCLUSION_EVAL_PROMPT.format(
                conclusion_fol=conclusion_fol, predicates=sig_block
            )}],
            max_tokens=100,
        )
        raw = response.strip()
        # Extract first line that looks like a Prolog goal (lowercase start, no spaces in atoms)
        for ln in raw.split("\n"):
            ln = ln.strip().rstrip(".")
            if not ln or len(ln) > 200:
                continue
            # Valid Prolog goal: starts with lowercase or '(' or '\+'
            if re.match(r'^[a-z\(\\]', ln) and "'" not in ln[:30]:
                goal = ln
                break
        else:
            goal = raw.split("\n")[0].strip().rstrip(".")
    if not goal or goal.lower() in ("fail", "false"):
        return "Uncertain"
    # sanitize
    dangerous = ["assert", "retract", "abolish", "consult", "halt"]
    if any(d in goal.lower() for d in dangerous):
        return "Uncertain"
    # Lowercase predicate names (LLM sometimes generates CamelCase like Perform(bonnie,...))
    goal = re.sub(r'\b([A-Z][a-zA-Z0-9_]*)\s*\(', lambda m: m.group(1).lower() + '(', goal)
    def safe_prolog_query(q_str: str) -> list:
        """Run a Prolog query with depth limit and timeout."""
        wrapped = f"catch(call_with_depth_limit(({q_str}), 20, _), _, fail)"
        return _prolog_query_with_timeout(prolog, wrapped, timeout_sec=2)

    try:
        pos = safe_prolog_query(goal)
        if pos:
            neg = safe_prolog_query(f"\\+({goal})")
            return "False" if neg else "True"
        else:
            neg = safe_prolog_query(f"\\+({goal})")
            return "False" if neg else "Uncertain"
    except Exception as e:
        logger.debug(f"evaluate_conclusion failed for '{goal[:60]}': {e}")
        return "Uncertain"


def compute_ochiai_scores(
    coverage_matrix: dict[str, list[str | None]],
    pass_fail: list[bool],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    n_failing = sum(1 for p in pass_fail if not p)
    for pred_key, outcomes in coverage_matrix.items():
        if not any(o == "unify_failed" for o in outcomes):
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


def repair_item(
    premises_text: str, kb_clauses: list[str], item: str, reason: str
) -> list[str]:
    kb_str = "\n".join(kb_clauses[:40])
    response, _ = llm_call(
        [{"role": "user", "content": REPAIR_PROMPT.format(
            premises=premises_text, kb=kb_str, item=item, reason=reason
        )}],
        max_tokens=400,
    )
    return _parse_clauses_from_text(response)


# ── Main pipeline ─────────────────────────────────────────────────────────────
def run_dual_sbfl_pipeline(
    example: dict,
    prolog,
    flat_mode: bool = False,
) -> dict[str, Any]:
    """Dual-signal SBFL pipeline. flat_mode=True for flat-SBFL ablation."""
    premises_text = example["premises"]
    all_preds: list[tuple[str, int]] = []

    # LLM Call 1: Extract KB
    try:
        clauses = extract_kb(premises_text)
    except BudgetExceededError:
        raise
    except Exception as e:
        logger.warning(f"KB extraction failed: {e}")
        clauses = []

    # LLM Call 2: Generate oracle queries + answers
    try:
        oracle_questions = generate_oracle_queries(premises_text)
    except BudgetExceededError:
        raise
    except Exception as e:
        logger.warning(f"Oracle query gen failed: {e}")
        oracle_questions = []

    try:
        oracle_answers = generate_oracle_answers(premises_text, oracle_questions) if oracle_questions else []
    except BudgetExceededError:
        raise
    except Exception as e:
        logger.warning(f"Oracle answer gen failed: {e}")
        oracle_answers = ["unknown"] * len(oracle_questions)

    pass_fail = [a == "yes" for a in oracle_answers]

    # Convert oracle questions to Prolog goals (batch: 1 LLM call)
    pred_names = extract_predicate_names_from_clauses(clauses)
    pred_sigs = [f"{n}/{a}" for n, a in pred_names]
    try:
        prolog_goals = nl_queries_to_prolog_batch(oracle_questions, pred_sigs)
    except BudgetExceededError:
        raise
    except Exception:
        prolog_goals = ["fail"] * len(oracle_questions)

    # SBFL Repair Loop
    current_clauses = clauses[:]
    all_repairs: list[dict] = []
    final_ochiai: dict[str, float] = {}
    final_missing: dict[str, int] = {}

    for round_idx in range(N_REPAIR_ROUNDS):
        if COST_TRACKER["total"] >= MAX_BUDGET_USD:
            break
        if not current_clauses:
            break

        pred_names = extract_predicate_names_from_clauses(current_clauses)
        clear_kb(prolog, all_preds)
        load_kb_into_prolog(prolog, current_clauses)
        all_preds = pred_names
        pred_sigs_cur = [f"{n}/{a}" for n, a in pred_names]

        n_queries = len(prolog_goals)
        coverage_matrix: dict[str, list[str | None]] = {}
        all_failed_subgoals: list[str] = []

        for i, goal in enumerate(prolog_goals):
            if goal == "fail":
                continue
            result = run_oracle_query_with_coverage(prolog, goal)
            all_failed_subgoals.extend(result["failed_subgoals"])
            for pred_key, outcomes in result["coverage"].items():
                if pred_key not in coverage_matrix:
                    coverage_matrix[pred_key] = [None] * n_queries
                # use dominant outcome
                worst = outcomes[-1] if outcomes else None
                coverage_matrix[pred_key][i] = worst

        if flat_mode:
            # Binary coverage: any access = 1, no access = 0
            # Treat as ochiai with binary accessed/not
            flat_ochiai: dict[str, float] = {}
            n_failing = sum(1 for p in pass_fail if not p)
            for pred_key, outcomes in coverage_matrix.items():
                ef = sum(1 for i, o in enumerate(outcomes) if o is not None and not pass_fail[i])
                ep = sum(1 for i, o in enumerate(outcomes) if o is not None and pass_fail[i])
                nf = n_failing - ef
                denom = math.sqrt((ef + nf) * (ef + ep))
                flat_ochiai[pred_key] = ef / denom if denom > 0 else 0.0
            ochiai = flat_ochiai
            missing: dict[str, int] = {}  # no sub-goal harvesting
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
                    premises_text, current_clauses,
                    item_info["item"],
                    f"Type: {item_info['type']}, Score: {item_info['score']:.3f}",
                )
            except BudgetExceededError:
                raise
            except Exception as e:
                logger.warning(f"Repair failed for {item_info['item']}: {e}")
                continue
            if new_clauses:
                if item_info["type"] == "erroneous":
                    pred_name = item_info["item"].split("/")[0]
                    current_clauses = [
                        c for c in current_clauses
                        if not re.match(r"^" + re.escape(pred_name) + r"[\s(]", c.split(":-")[0].strip())
                    ]
                current_clauses.extend(new_clauses)
                all_repairs.append({
                    "item": item_info["item"],
                    "type": item_info["type"],
                    "round": round_idx,
                    "new_clauses_count": len(new_clauses),
                })

    # Final evaluation
    pred_names = extract_predicate_names_from_clauses(current_clauses)
    clear_kb(prolog, all_preds)
    load_kb_into_prolog(prolog, current_clauses)
    all_preds = pred_names
    pred_sigs_final = [f"{n}/{a}" for n, a in pred_names]

    predicted_label = evaluate_conclusion(
        prolog, example.get("conclusion-FOL", ""), current_clauses, pred_sigs_final
    )

    # clean up
    clear_kb(prolog, all_preds)

    return {
        "predicted_label": predicted_label,
        "num_repairs_applied": len(all_repairs),
        "ochiai_scores_top5": sorted(
            [(k, v) for k, v in final_ochiai.items()], key=lambda x: -x[1]
        )[:5],
        "subgoal_harvest_top5": sorted(
            [(k, v) for k, v in final_missing.items()], key=lambda x: -x[1]
        )[:5] if not flat_mode else [],
        "repairs": all_repairs,
        "kb_size": len(current_clauses),
    }


# ── Baselines ─────────────────────────────────────────────────────────────────
def run_oneshot_baseline(example: dict, prolog) -> dict[str, Any]:
    premises_text = example["premises"]
    try:
        clauses = extract_kb(premises_text)
    except BudgetExceededError:
        raise
    except Exception:
        clauses = []
    pred_names = extract_predicate_names_from_clauses(clauses)
    load_kb_into_prolog(prolog, clauses)
    pred_sigs = [f"{n}/{a}" for n, a in pred_names]
    predicted = evaluate_conclusion(
        prolog, example.get("conclusion-FOL", ""), clauses, pred_sigs
    )
    clear_kb(prolog, pred_names)
    return {"predicted_label": predicted, "method": "oneshot"}


def run_cot_baseline(example: dict) -> dict[str, Any]:
    premises_text = example["premises"]
    conclusion = example.get("conclusion", "")
    prompt = (
        f"Given the following premises:\n{premises_text}\n\n"
        f"Conclusion: {conclusion}\n\n"
        "Think step by step, then answer with exactly one word: True, False, or Uncertain."
    )
    try:
        response, _ = llm_call(
            [{"role": "user", "content": prompt}], max_tokens=400
        )
    except BudgetExceededError:
        raise
    except Exception:
        return {"predicted_label": "Uncertain", "method": "cot"}
    for word in reversed(response.split()):
        w = word.strip(".,!?:").capitalize()
        if w in ("True", "False", "Uncertain"):
            return {"predicted_label": w, "method": "cot"}
    return {"predicted_label": "Uncertain", "method": "cot"}


def run_selfrefine_baseline(
    example: dict, prolog, n_rounds: int = 1
) -> dict[str, Any]:
    premises_text = example["premises"]
    try:
        oracle_qs = generate_oracle_queries(premises_text, n=8)
        oracle_as = generate_oracle_answers(premises_text, oracle_qs)
        clauses = extract_kb(premises_text)
    except BudgetExceededError:
        raise
    except Exception as e:
        return {"predicted_label": "Uncertain", "method": "selfrefine"}

    all_preds: list[tuple[str, int]] = []
    for r in range(n_rounds):
        if COST_TRACKER["total"] >= MAX_BUDGET_USD:
            break
        pred_names = extract_predicate_names_from_clauses(clauses)
        clear_kb(prolog, all_preds)
        load_kb_into_prolog(prolog, clauses)
        all_preds = pred_names
        pred_sigs = [f"{n}/{a}" for n, a in pred_names]
        goals = nl_queries_to_prolog_batch(oracle_qs, pred_sigs)
        passed = 0
        for i, goal in enumerate(goals):
            if goal == "fail":
                continue
            try:
                wrapped = f"catch(call_with_depth_limit(({goal}), 20, _), _, fail)"
                res = _prolog_query_with_timeout(prolog, wrapped, timeout_sec=2)
                if (res and oracle_as[i] == "yes") or (not res and oracle_as[i] != "yes"):
                    passed += 1
            except Exception:
                pass
        pass_rate = passed / max(len(oracle_qs), 1)
        refine_prompt = (
            f"Your Prolog KB achieved {pass_rate:.0%} on oracle queries.\n"
            f"Premises:\n{premises_text}\n\n"
            f"Current KB:\n{chr(10).join(clauses[:40])}\n\n"
            "Regenerate the KB to improve coverage. Output ONLY Prolog clauses."
        )
        try:
            response, _ = llm_call(
                [{"role": "user", "content": refine_prompt}], max_tokens=600
            )
            new_clauses = _parse_clauses_from_text(response)
            if new_clauses:
                clauses = new_clauses
        except BudgetExceededError:
            raise
        except Exception:
            pass

    pred_names = extract_predicate_names_from_clauses(clauses)
    clear_kb(prolog, all_preds)
    load_kb_into_prolog(prolog, clauses)
    all_preds = pred_names
    pred_sigs = [f"{n}/{a}" for n, a in pred_names]
    predicted = evaluate_conclusion(
        prolog, example.get("conclusion-FOL", ""), clauses, pred_sigs
    )
    clear_kb(prolog, all_preds)
    return {"predicted_label": predicted, "method": "selfrefine"}


def run_flat_sbfl_baseline(example: dict, prolog) -> dict[str, Any]:
    result = run_dual_sbfl_pipeline(example, prolog, flat_mode=True)
    result["method"] = "flat_sbfl"
    return result


# ── Main ──────────────────────────────────────────────────────────────────────
@logger.catch(reraise=True)
def main() -> None:
    logger.info("Loading FOLIO validation dataset (tasksource/folio)")
    ds = load_dataset("tasksource/folio", split="validation")
    all_examples = list(ds)
    logger.info(f"Loaded {len(all_examples)} examples")

    # Normalize labels
    def normalize_label(lbl: str) -> str:
        m = {"true": "True", "false": "False", "uncertain": "Uncertain", "unknown": "Uncertain"}
        return m.get(str(lbl).lower().strip(), "Uncertain")

    for ex in all_examples:
        ex["label"] = normalize_label(ex["label"])

    examples = all_examples[:MAX_EXAMPLES]
    logger.info(f"Running on {len(examples)} examples (MAX={MAX_EXAMPLES})")

    get_prolog()  # Initialize once

    output_examples: list[dict] = []
    method_names = ["dual_sbfl", "oneshot", "cot", "selfrefine", "flat_sbfl"]
    correct: dict[str, int] = {m: 0 for m in method_names}
    total_repairs = 0

    for i, ex in enumerate(tqdm(examples, desc="Processing")):
        if COST_TRACKER["total"] >= MAX_BUDGET_USD:
            logger.warning(f"Budget exceeded at example {i}. Stopping.")
            break

        gold = ex["label"]
        ex_id = ex.get("story_id", i)

        results_by_method: dict[str, dict] = {}

        prolog = get_prolog()

        # 1. Dual-SBFL (our method)
        try:
            r = run_dual_sbfl_pipeline(ex, prolog, flat_mode=False)
            results_by_method["dual_sbfl"] = r
        except BudgetExceededError:
            logger.warning(f"Budget exceeded at example {i}, stopping")
            break
        except Exception as e:
            logger.error(f"dual_sbfl failed on ex {i}: {e}")
            results_by_method["dual_sbfl"] = {"predicted_label": "Uncertain", "error": str(e)}
            reset_prolog()

        # 2. Oneshot
        try:
            prolog = get_prolog()
            r = run_oneshot_baseline(ex, prolog)
            results_by_method["oneshot"] = r
        except BudgetExceededError:
            logger.warning("Budget exceeded, stopping")
            break
        except Exception as e:
            logger.error(f"oneshot failed on ex {i}: {e}")
            results_by_method["oneshot"] = {"predicted_label": "Uncertain"}
            reset_prolog()

        # 3. CoT (no Prolog needed)
        try:
            r = run_cot_baseline(ex)
            results_by_method["cot"] = r
        except BudgetExceededError:
            logger.warning("Budget exceeded, stopping")
            break
        except Exception as e:
            logger.error(f"cot failed on ex {i}: {e}")
            results_by_method["cot"] = {"predicted_label": "Uncertain"}

        # 4. Self-refine
        try:
            prolog = get_prolog()
            r = run_selfrefine_baseline(ex, prolog)
            results_by_method["selfrefine"] = r
        except BudgetExceededError:
            logger.warning("Budget exceeded, stopping")
            break
        except Exception as e:
            logger.error(f"selfrefine failed on ex {i}: {e}")
            results_by_method["selfrefine"] = {"predicted_label": "Uncertain"}
            reset_prolog()

        # 5. Flat SBFL (ablation)
        try:
            prolog = get_prolog()
            r = run_flat_sbfl_baseline(ex, prolog)
            results_by_method["flat_sbfl"] = r
        except BudgetExceededError:
            logger.warning("Budget exceeded, stopping")
            break
        except Exception as e:
            logger.error(f"flat_sbfl failed on ex {i}: {e}")
            results_by_method["flat_sbfl"] = {"predicted_label": "Uncertain"}
            reset_prolog()

        # Accumulate
        for m in method_names:
            pred = results_by_method.get(m, {}).get("predicted_label", "Uncertain")
            if pred == gold:
                correct[m] += 1

        dual_r = results_by_method.get("dual_sbfl", {})
        total_repairs += dual_r.get("num_repairs_applied", 0)

        # Build output example in exp_gen_sol_out schema
        out_ex: dict[str, Any] = {
            "input": ex["premises"],
            "output": gold,
            "predict_dual_sbfl": results_by_method.get("dual_sbfl", {}).get("predicted_label", "Uncertain"),
            "predict_oneshot": results_by_method.get("oneshot", {}).get("predicted_label", "Uncertain"),
            "predict_cot": results_by_method.get("cot", {}).get("predicted_label", "Uncertain"),
            "predict_selfrefine": results_by_method.get("selfrefine", {}).get("predicted_label", "Uncertain"),
            "predict_flat_sbfl": results_by_method.get("flat_sbfl", {}).get("predicted_label", "Uncertain"),
            "metadata_story_id": str(ex_id),
            "metadata_conclusion": ex.get("conclusion", ""),
            "metadata_conclusion_fol": ex.get("conclusion-FOL", ""),
            "metadata_num_repairs": str(dual_r.get("num_repairs_applied", 0)),
            "metadata_kb_size": str(dual_r.get("kb_size", 0)),
            "metadata_ochiai_top5": json.dumps(dual_r.get("ochiai_scores_top5", [])),
            "metadata_subgoal_top5": json.dumps(dual_r.get("subgoal_harvest_top5", [])),
            "metadata_cumulative_cost_usd": f"{COST_TRACKER['total']:.4f}",
        }
        output_examples.append(out_ex)

        if (i + 1) % 10 == 0:
            n_done = len(output_examples)
            accs = {m: correct[m] / n_done for m in method_names}
            logger.info(
                f"[{i+1}/{len(examples)}] dual_sbfl={accs['dual_sbfl']:.3f} "
                f"oneshot={accs['oneshot']:.3f} cot={accs['cot']:.3f} "
                f"cost=${COST_TRACKER['total']:.2f}"
            )

        gc.collect()

    n_done = len(output_examples)
    summary_metrics = {
        "n_examples": n_done,
        "total_cost_usd": round(COST_TRACKER["total"], 4),
        "total_llm_calls": COST_TRACKER["calls"],
        "avg_repairs_per_example": round(total_repairs / max(n_done, 1), 3),
    }
    for m in method_names:
        summary_metrics[f"{m}_accuracy"] = round(correct[m] / max(n_done, 1), 4)

    logger.info("=" * 60)
    logger.info("SUMMARY:")
    for k, v in summary_metrics.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 60)

    output = {
        "metadata": {
            "method_name": "dual_signal_sbfl",
            "description": "Dual-signal SBFL (Ochiai + sub-goal harvesting) for Prolog KB repair on FOLIO",
            "model": MODEL,
            "parameters": {
                "n_oracle_queries": N_ORACLE_QUERIES,
                "n_repair_rounds": N_REPAIR_ROUNDS,
                "k_repair_targets": K_REPAIR_TARGETS,
            },
            "summary": summary_metrics,
        },
        "datasets": [
            {
                "dataset": "folio_validation",
                "examples": output_examples,
            }
        ],
    }

    out_path = WORKSPACE / "method_out.json"
    out_path.write_text(json.dumps(output, indent=2))
    logger.info(f"Saved {n_done} results to {out_path}")
    logger.info(f"Final cost: ${COST_TRACKER['total']:.4f}")


if __name__ == "__main__":
    main()
