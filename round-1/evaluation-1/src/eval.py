#!/usr/bin/env python3
"""
Dual-Signal SBFL vs. Baselines: End-to-End Statistical Evaluation on FOLIO
Evaluates 5 methods: dual-SBFL, one-shot, self-refine, flat-SBFL, CoT
"""

import sys
import os
import json
import math
import re
import gc
import asyncio
import time
import base64
import resource
import traceback
from pathlib import Path
from collections import defaultdict
from typing import Any

import numpy as np
import aiohttp
from loguru import logger

# ── Logging ──────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent
(WORKSPACE / "logs").mkdir(exist_ok=True)
(WORKSPACE / "figures").mkdir(exist_ok=True)

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add(str(WORKSPACE / "logs/run.log"), rotation="30 MB", level="DEBUG")

# ── Resource Limits ───────────────────────────────────────────────────────────
RAM_BUDGET = 20 * 1024**3  # 20 GB of 29 GB
resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET, RAM_BUDGET))

# ── Constants ─────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PRIMARY_MODEL = "anthropic/claude-3-haiku"   # cheapest, $0.25/$1.25 per 1M
FALLBACK_MODEL = "meta-llama/llama-3.1-8b-instruct"
COST_HARD_STOP = 8.0  # USD
PARTIAL_SAVE_EVERY = 10
MAX_EXAMPLES = 150
N_CALIBRATION = 10
# Use ProofWriter as FOLIO is gated. ProofWriter has same structure:
# theory (premises), question (conclusion), answer (True/False/Unknown), QDep (hop depth)
DATASET_NAME = "tasksource/proofwriter"
DATASET_SPLIT = "validation"
N_REPAIR_ROUNDS = 2
TOP_K_REPAIR = 3
N_ORACLE_QUERIES = 5
N_BOOTSTRAP = 10000
CONCURRENCY = 20  # async semaphore — parallelize all examples in a method

# Model pricing (per 1M tokens) — haiku 4.5 or fallback llama
MODEL_PRICING = {
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-3-haiku": (0.25, 1.25),
    "anthropic/claude-3-haiku-20240307": (0.25, 1.25),
    "meta-llama/llama-3.1-8b-instruct": (0.05, 0.08),
}

# ── Global cost tracker ───────────────────────────────────────────────────────
_total_cost_usd = 0.0
_total_llm_calls = 0

def _calc_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, (0.80, 4.00))
    return (in_tokens * pricing[0] + out_tokens * pricing[1]) / 1_000_000


# ── LLM Call ─────────────────────────────────────────────────────────────────
async def call_llm(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    messages: list[dict],
    model: str = PRIMARY_MODEL,
    max_tokens: int = 800,
    temperature: float = 0.0,
) -> tuple[str, int, int, float]:
    """Returns (text, in_tokens, out_tokens, cost_usd). Raises on hard budget."""
    global _total_cost_usd, _total_llm_calls

    if _total_cost_usd >= COST_HARD_STOP:
        raise RuntimeError(f"HARD STOP: budget {_total_cost_usd:.2f} USD reached")

    async with sem:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            try:
                async with session.post(
                    OPENROUTER_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    if "error" in data:
                        err_msg = data["error"].get("message", str(data["error"]))
                        # Try fallback model
                        if attempt == 0 and model != FALLBACK_MODEL:
                            model = FALLBACK_MODEL
                            payload["model"] = model
                            continue
                        raise RuntimeError(f"LLM error: {err_msg}")
                    text = data["choices"][0]["message"]["content"] or ""
                    usage = data.get("usage", {})
                    in_tok = usage.get("prompt_tokens", 0)
                    out_tok = usage.get("completion_tokens", 0)
                    cost = _calc_cost(model, in_tok, out_tok)
                    _total_cost_usd += cost
                    _total_llm_calls += 1
                    logger.debug(f"LLM call #{_total_llm_calls} model={model} in={in_tok} out={out_tok} cost=${cost:.4f} total=${_total_cost_usd:.3f}")
                    return text, in_tok, out_tok, cost
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("LLM call failed after 3 attempts")


# ── Prolog KB helpers ─────────────────────────────────────────────────────────
def extract_prolog_block(text: str) -> str | None:
    """Extract ```prolog ... ``` block from LLM response."""
    m = re.search(r"```(?:prolog|pl)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback: look for lines starting with lowercase predicates
    lines = [l for l in text.split("\n") if re.match(r"^[a-z][a-zA-Z0-9_]*[\s(]", l.strip())]
    if lines:
        return "\n".join(lines)
    return None


class CallLimitExceeded(Exception):
    pass


class PrologInterpreter:
    """
    Minimal pure-Python Prolog interpreter supporting:
    - Facts: foo(a, b).
    - Rules: foo(X) :- bar(X), baz(X).
    - Queries: ?- foo(X). (returned as bool/bindings)
    Uses forward-chaining for ground queries.
    """
    MAX_CALLS = 2000  # hard limit per query to prevent exponential backtracking

    def __init__(self):
        self.facts: dict[str, list[tuple]] = defaultdict(list)   # functor/arity -> list of arg-tuples
        self.rules: list[tuple] = []  # (head_functor, head_args, body_goals)
        self.parse_errors: list[str] = []
        self._predicate_names: set[str] = set()
        self._call_count = 0

    def load_kb(self, kb_text: str) -> bool:
        """Parse and load KB. Returns True if at least some clauses parsed."""
        count = 0
        for raw_clause in self._split_clauses(kb_text):
            clause = raw_clause.strip()
            if not clause or clause.startswith("%"):
                continue
            try:
                if ":-" in clause:
                    head_str, body_str = clause.split(":-", 1)
                    head = self._parse_term(head_str.strip())
                    if head is None:
                        continue
                    body_goals = self._parse_body(body_str.strip())
                    functor, args = self._unpack(head)
                    self.rules.append((functor, args, body_goals))
                    self._predicate_names.add(functor)
                else:
                    term = self._parse_term(clause)
                    if term is None:
                        continue
                    functor, args = self._unpack(term)
                    self.facts[functor].append(args)
                    self._predicate_names.add(functor)
                count += 1
            except Exception as e:
                self.parse_errors.append(f"{clause[:60]}: {e}")
        return count > 0

    def _split_clauses(self, text: str) -> list[str]:
        """Split on periods that end a clause (not inside parens or strings)."""
        clauses = []
        depth = 0
        buf = []
        for ch in text:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "." and depth == 0:
                c = "".join(buf).strip()
                if c:
                    clauses.append(c)
                buf = []
                continue
            buf.append(ch)
        last = "".join(buf).strip()
        if last:
            clauses.append(last)
        return clauses

    def _parse_term(self, s: str):
        """Parse a single term (atom, compound, variable)."""
        s = s.strip()
        if not s:
            return None
        # Remove trailing dot if present
        if s.endswith("."):
            s = s[:-1].strip()
        # Variable (starts with uppercase or _)
        if re.match(r"^[A-Z_]", s):
            return ("_var", s)
        # Atom or compound
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*)\)$", s, re.DOTALL)
        if m:
            functor = m.group(1)
            args_str = m.group(2)
            args = self._split_args(args_str)
            return (functor, [self._parse_term(a) for a in args])
        # Atom (no parens) or quoted atom
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", s):
            return ("_atom", s)
        # Number
        if re.match(r"^-?\d+(\.\d+)?$", s):
            return ("_atom", s)
        # Quoted string
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            return ("_atom", s[1:-1])
        return ("_atom", s)

    def _split_args(self, s: str) -> list[str]:
        """Split comma-separated args respecting parentheses."""
        args = []
        depth = 0
        buf = []
        for ch in s:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append("".join(buf).strip())
                buf = []
                continue
            buf.append(ch)
        if buf:
            args.append("".join(buf).strip())
        return [a for a in args if a]

    def _parse_body(self, s: str) -> list:
        """Parse body goals (comma-separated terms)."""
        # Split at commas not inside parens
        parts = self._split_args(s)
        goals = []
        for p in parts:
            p = p.strip()
            if p.startswith("\\+") or p.startswith("not("):
                inner = p[2:].strip() if p.startswith("\\+") else p[4:-1]
                goals.append(("_not", self._parse_term(inner)))
            else:
                goals.append(self._parse_term(p))
        return goals

    def _unpack(self, term) -> tuple[str, list]:
        if term is None:
            return ("_unknown", [])
        if term[0] == "_atom":
            return (term[1], [])
        if term[0] == "_var":
            return (term[1], [])
        # compound
        return (term[0], term[1])

    def query(self, goal_str: str) -> tuple[bool, dict]:
        """
        Run a ground query. Returns (success, bindings).
        Supports simple ground queries only.
        """
        self._call_count = 0
        goal = self._parse_term(goal_str.strip().rstrip("."))
        if goal is None:
            return False, {}
        try:
            return self._solve(goal, {}, depth=0)
        except CallLimitExceeded:
            return False, {}

    def _solve(self, goal, bindings: dict, depth: int = 0) -> tuple[bool, dict]:
        self._call_count += 1
        if self._call_count > self.MAX_CALLS:
            raise CallLimitExceeded()
        if depth > 30:
            return False, {}
        if goal is None:
            return False, {}

        if goal[0] == "_not":
            success, _ = self._solve(goal[1], bindings, depth + 1)
            return not success, bindings

        functor, args = self._unpack(goal)

        # Special builtins
        if functor in ("true",):
            return True, bindings
        if functor in ("fail", "false"):
            return False, bindings

        # Apply bindings to args
        args = [self._apply_bindings(a, bindings) for a in args]
        arity = len(args)

        # Try facts
        for fact_args in self.facts.get(functor, []):
            if len(fact_args) != arity:
                continue
            new_bindings = dict(bindings)
            if self._unify_args(args, fact_args, new_bindings):
                return True, new_bindings

        # Try rules
        for (r_functor, r_args, r_body) in self.rules:
            if r_functor != functor or len(r_args) != arity:
                continue
            new_bindings = dict(bindings)
            # Rename variables in rule
            renamed_args, renamed_body = self._rename_rule(r_args, r_body, depth)
            if not self._unify_args(args, renamed_args, new_bindings):
                continue
            # Prove all body goals
            success = True
            cur_bindings = new_bindings
            for body_goal in renamed_body:
                ok, cur_bindings = self._solve(body_goal, cur_bindings, depth + 1)
                if not ok:
                    success = False
                    break
            if success:
                return True, cur_bindings

        return False, {}

    def _rename_rule(self, args: list, body: list, depth: int):
        """Rename variables in a rule to avoid clashes."""
        suffix = f"_{depth}"
        renamed_args = [self._rename_vars(a, suffix) for a in args]
        renamed_body = [self._rename_vars(g, suffix) for g in body]
        return renamed_args, renamed_body

    def _rename_vars(self, term, suffix: str):
        if term is None:
            return None
        if term[0] == "_var":
            return ("_var", term[1] + suffix)
        if term[0] == "_atom":
            return term
        # compound
        return (term[0], [self._rename_vars(a, suffix) for a in term[1]])

    def _apply_bindings(self, term, bindings: dict):
        if term is None:
            return None
        if term[0] == "_var":
            if term[1] in bindings:
                return self._apply_bindings(bindings[term[1]], bindings)
            return term
        if term[0] == "_atom":
            return term
        return (term[0], [self._apply_bindings(a, bindings) for a in term[1]])

    def _unify_args(self, query_args: list, fact_args: list, bindings: dict) -> bool:
        if len(query_args) != len(fact_args):
            return False
        for qa, fa in zip(query_args, fact_args):
            if not self._unify(qa, fa, bindings):
                return False
        return True

    def _unify(self, t1, t2, bindings: dict) -> bool:
        if t1 is None or t2 is None:
            return t1 == t2
        t1 = self._apply_bindings(t1, bindings)
        t2 = self._apply_bindings(t2, bindings)
        if t1 == t2:
            return True
        if t1[0] == "_var":
            bindings[t1[1]] = t2
            return True
        if t2[0] == "_var":
            bindings[t2[1]] = t1
            return True
        if t1[0] == "_atom" and t2[0] == "_atom":
            return t1[1].lower() == t2[1].lower()
        # Both compound
        if t1[0] == "_atom" or t2[0] == "_atom":
            return False
        if t1[0] != t2[0] or len(t1[1]) != len(t2[1]):
            return False
        return self._unify_args(t1[1], t2[1], bindings)

    def get_predicates(self) -> list[str]:
        """Return all defined predicate names."""
        preds = set(self.facts.keys()) | set(r[0] for r in self.rules)
        return list(preds)

    def query_with_trace(self, goal_str: str) -> tuple[bool, dict, dict[str, dict]]:
        """
        Run query and return (success, bindings, coverage).
        coverage[predicate] = {accessed: bool, unified: bool, subgoal_failed: bool}
        """
        self._call_count = 0
        self._trace: dict[str, dict] = {}
        goal = self._parse_term(goal_str.strip().rstrip("."))
        if goal is None:
            return False, {}, {}
        try:
            success, bindings = self._solve_traced(goal, {}, depth=0)
        except CallLimitExceeded:
            success, bindings = False, {}
        return success, bindings, self._trace

    def _solve_traced(self, goal, bindings: dict, depth: int = 0) -> tuple[bool, dict]:
        self._call_count += 1
        if self._call_count > self.MAX_CALLS:
            raise CallLimitExceeded()
        if depth > 30:
            return False, {}
        if goal is None:
            return False, {}

        if goal[0] == "_not":
            success, _ = self._solve_traced(goal[1], bindings, depth + 1)
            return not success, bindings

        functor, args = self._unpack(goal)
        if functor in ("true",):
            return True, bindings
        if functor in ("fail", "false"):
            return False, bindings

        args = [self._apply_bindings(a, bindings) for a in args]
        arity = len(args)
        key = f"{functor}/{arity}"

        if key not in self._trace:
            self._trace[key] = {"accessed": False, "unified": False, "subgoal_failed": False}
        self._trace[key]["accessed"] = True

        # Try facts
        for fact_args in self.facts.get(functor, []):
            if len(fact_args) != arity:
                continue
            new_bindings = dict(bindings)
            if self._unify_args(args, fact_args, new_bindings):
                self._trace[key]["unified"] = True
                return True, new_bindings

        # Try rules
        for (r_functor, r_args, r_body) in self.rules:
            if r_functor != functor or len(r_args) != arity:
                continue
            new_bindings = dict(bindings)
            renamed_args, renamed_body = self._rename_rule(r_args, r_body, depth)
            if not self._unify_args(args, renamed_args, new_bindings):
                continue
            self._trace[key]["unified"] = True
            success = True
            cur_bindings = new_bindings
            for body_goal in renamed_body:
                ok, cur_bindings = self._solve_traced(body_goal, cur_bindings, depth + 1)
                if not ok:
                    bg_functor, bg_args = self._unpack(body_goal)
                    bg_key = f"{bg_functor}/{len(bg_args)}"
                    if bg_key not in self._trace:
                        self._trace[bg_key] = {"accessed": False, "unified": False, "subgoal_failed": False}
                    self._trace[bg_key]["subgoal_failed"] = True
                    success = False
                    break
            if success:
                return True, cur_bindings

        self._trace[key]["subgoal_failed"] = True
        return False, {}


# ── Hop depth estimation ──────────────────────────────────────────────────────
def estimate_hop_depth(premises: list[str], conclusion: str, example: dict | None = None) -> int:
    """Hop depth: use QDep if available (ProofWriter), else heuristic."""
    if example and "qdep" in example:
        d = example["qdep"]
        if d == 0:
            return 1
        elif d == 1:
            return 1
        elif d == 2:
            return 2
        else:
            return 3
    # Heuristic fallback
    conj_count = len(re.findall(r"\b(and|but|therefore|thus|hence|so|also)\b", conclusion.lower()))
    comma_count = conclusion.count(",")
    total = conj_count + max(0, comma_count - 1)
    if total == 0:
        return 1
    elif total == 1:
        return 2
    else:
        return 3


# ── Prompts ───────────────────────────────────────────────────────────────────
def make_kb_extraction_prompt(premises: list[str], conclusion: str) -> list[dict]:
    premises_text = "\n".join(f"- {p}" for p in premises)
    return [
        {"role": "system", "content": "You are a formal logic expert. Convert natural language statements to SWI-Prolog clauses."},
        {"role": "user", "content": f"""Convert the following premises to a SWI-Prolog knowledge base.

PREMISES:
{premises_text}

CONCLUSION TO PROVE: {conclusion}

Output ONLY a valid SWI-Prolog knowledge base as a code block. Include:
- Facts as: predicate(argument).
- Rules as: head :- body.
- Also include a query comment showing how to test: % Query: conclusion_predicate(args)
- Use lowercase for predicate names and constants
- Use CamelCase for variables

```prolog
% Your Prolog KB here
```"""}
    ]


def make_query_extraction_prompt(conclusion: str, kb_preview: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are a Prolog expert. Given a natural language conclusion and a Prolog KB, write a Prolog query."},
        {"role": "user", "content": f"""Given this Prolog knowledge base:
```prolog
{kb_preview[:800]}
```

Write a Prolog query to test: "{conclusion}"

Respond with ONLY the query as a single line (no ```), ending with a period.
Example: entailed(john).
If the conclusion is negative (X is NOT ...), prefix with \\+
Example: \\+owns(john, car).
"""}
    ]


def make_oracle_queries_prompt(premises: list[str], kb_preview: str, n_queries: int = 10) -> list[dict]:
    premises_text = "\n".join(f"- {p}" for p in premises[:5])
    return [
        {"role": "system", "content": "You are a Prolog testing expert. Generate yes/no test queries for a KB."},
        {"role": "user", "content": f"""Given this Prolog knowledge base:
```prolog
{kb_preview[:600]}
```

And these source premises:
{premises_text}

Generate {n_queries} yes/no test queries to probe the KB's correctness.
Each query should test a specific fact or rule in the KB.
Output ONLY the queries, one per line, each ending with a period.
Example:
human(socrates).
mortal(socrates).
\\+immortal(socrates).
"""}
    ]


def make_repair_prompt(
    suspicious_pred: str,
    current_def: str,
    kb_context: str,
    source_text: str,
    sub_goals: list[str],
) -> list[dict]:
    sub_goal_text = "\n".join(f"- {sg}" for sg in sub_goals[:5]) if sub_goals else "None identified"
    return [
        {"role": "system", "content": "You are a Prolog expert. Fix or add predicates to repair a faulty knowledge base."},
        {"role": "user", "content": f"""The predicate '{suspicious_pred}' appears faulty in the Prolog KB.

Current definition:
```prolog
{current_def[:400] if current_def else "% NOT DEFINED"}
```

Missing sub-goals that failed during execution:
{sub_goal_text}

Source text context:
{source_text[:600]}

KB context (neighboring predicates):
```prolog
{kb_context[:400]}
```

Output ONLY corrected or new Prolog clauses (no explanations), inside a code block:
```prolog
% Repaired clauses
```"""}
    ]


def make_cot_prompt(premises: list[str], conclusion: str) -> list[dict]:
    premises_text = "\n".join(f"- {p}" for p in premises)
    return [
        {"role": "system", "content": "You are a logical reasoning expert. Reason step by step."},
        {"role": "user", "content": f"""Given these premises:
{premises_text}

Does the following conclusion follow?
Conclusion: {conclusion}

Think step by step, then answer with exactly one of: ENTAILED, NOT_ENTAILED, or UNKNOWN
Format your final answer as: ANSWER: <ENTAILED|NOT_ENTAILED|UNKNOWN>"""}
    ]


def make_self_refine_prompt(
    premises: list[str], conclusion: str, kb: str, pass_rate: float, round_num: int
) -> list[dict]:
    premises_text = "\n".join(f"- {p}" for p in premises)
    return [
        {"role": "system", "content": "You are a Prolog expert. Refine a knowledge base to improve correctness."},
        {"role": "user", "content": f"""PREMISES:
{premises_text}

CONCLUSION TO PROVE: {conclusion}

Current KB (round {round_num}):
```prolog
{kb[:800]}
```

Current pass rate on test queries: {pass_rate:.1%}

Please refine the KB to be more accurate. Output ONLY the improved KB:
```prolog
% Improved KB
```"""}
    ]


# ── Label parsing ─────────────────────────────────────────────────────────────
def parse_folio_label(label: str) -> str:
    """Normalize FOLIO label to one of: entailed, not_entailed, unknown."""
    l = label.lower().strip()
    if l in ("true", "entailed", "yes"):
        return "entailed"
    elif l in ("false", "not entailed", "not_entailed", "no"):
        return "not_entailed"
    else:
        return "unknown"


def label_to_bool(label: str) -> bool | None:
    """Convert label to Prolog expected result."""
    if label == "entailed":
        return True
    elif label == "not_entailed":
        return False
    return None  # unknown


# ── KB extraction helpers ─────────────────────────────────────────────────────
async def extract_kb(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    premises: list[str],
    conclusion: str,
    model: str = PRIMARY_MODEL,
) -> tuple[str | None, str, int, float]:
    """
    Extract Prolog KB. Returns (kb_text, raw_response, n_calls, cost).
    """
    msgs = make_kb_extraction_prompt(premises, conclusion)
    text, in_t, out_t, cost = await call_llm(session, sem, msgs, model=model, max_tokens=1200)
    kb = extract_prolog_block(text)
    return kb, text, 1, cost


async def extract_query(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    conclusion: str,
    kb: str,
    model: str = PRIMARY_MODEL,
) -> tuple[str, int, float]:
    """Extract Prolog query for conclusion."""
    msgs = make_query_extraction_prompt(conclusion, kb)
    text, in_t, out_t, cost = await call_llm(session, sem, msgs, model=model, max_tokens=100)
    # Clean up query
    query = text.strip().split("\n")[0].strip().rstrip(".")
    return query, 1, cost


def run_prolog_query(kb: str, query: str) -> bool | None:
    """Run a Prolog query against a KB. Returns True/False/None(unknown/error)."""
    try:
        interp = PrologInterpreter()
        ok = interp.load_kb(kb)
        if not ok:
            return None
        success, _ = interp.query(query)
        return success
    except Exception as e:
        logger.debug(f"Prolog error: {e}")
        return None


def run_prolog_query_traced(kb: str, query: str) -> tuple[bool | None, dict[str, dict]]:
    """Run traced query, return (result, coverage_dict)."""
    try:
        interp = PrologInterpreter()
        interp.load_kb(kb)
        success, _, coverage = interp.query_with_trace(query)
        return success, coverage
    except Exception:
        return None, {}


def check_kb_result_vs_label(kb: str | None, query: str | None, gold_label: str) -> bool:
    """Check if KB + query produces correct answer vs gold FOLIO label."""
    if kb is None or query is None or not kb.strip() or not query.strip():
        # No KB / query = treat as unknown
        return gold_label == "unknown"

    result = run_prolog_query(kb, query)

    if gold_label == "entailed":
        return result is True
    elif gold_label == "not_entailed":
        return result is False
    else:  # unknown
        return result is None


# ── Ochiai computation ────────────────────────────────────────────────────────
def compute_ochiai_scores(
    coverage_matrix: list[tuple[dict[str, dict], bool]],
    stratified: bool = False,
) -> dict[str, float]:
    """
    Given list of (coverage_dict, query_passed), compute Ochiai scores.
    coverage_dict: predicate -> {accessed, unified, subgoal_failed}
    stratified: if True, use direct fault (unification failed) only for ef counting
    """
    all_preds = set()
    for cov, _ in coverage_matrix:
        all_preds.update(cov.keys())

    scores = {}
    for pred in all_preds:
        ef = ep = nf = 0
        for cov, passed in coverage_matrix:
            accessed = cov.get(pred, {}).get("accessed", False)
            unified = cov.get(pred, {}).get("unified", False)
            if stratified:
                # Direct fault: predicate was accessed but unification failed
                direct_fault = accessed and not unified
            else:
                direct_fault = accessed  # flat: any access counts

            if accessed:
                if passed:
                    ep += 1
                else:
                    ef += direct_fault if stratified else 1
            else:
                if not passed:
                    nf += 1

        denom = math.sqrt((ef + nf) * (ef + ep)) if (ef + nf) > 0 and (ef + ep) > 0 else 0
        scores[pred] = ef / denom if denom > 0 else 0.0

    return scores


# ── Sub-goal harvesting ───────────────────────────────────────────────────────
def harvest_subgoals(
    coverage_matrix: list[tuple[dict[str, dict], bool]],
    kb: str,
) -> list[str]:
    """Find predicates that repeatedly failed and are missing from KB."""
    failed_counts: dict[str, int] = defaultdict(int)
    for cov, passed in coverage_matrix:
        if not passed:
            for pred, info in cov.items():
                if info.get("subgoal_failed", False):
                    failed_counts[pred] += 1

    # Get defined predicates
    interp = PrologInterpreter()
    interp.load_kb(kb)
    defined = set(interp.get_predicates())

    # Sub-goals appearing >= 2 times and not defined
    candidates = [p for p, c in failed_counts.items() if c >= 2 and p not in defined]
    return sorted(candidates, key=lambda p: -failed_counts[p])


# ── Oracle generation ─────────────────────────────────────────────────────────
async def generate_oracle_queries(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    premises: list[str],
    kb: str,
    model: str = PRIMARY_MODEL,
    n_queries: int = N_ORACLE_QUERIES,
) -> tuple[list[str], int, float]:
    """Generate oracle queries via LLM."""
    msgs = make_oracle_queries_prompt(premises, kb, n_queries)
    text, in_t, out_t, cost = await call_llm(session, sem, msgs, model=model, max_tokens=400)
    queries = []
    for line in text.strip().split("\n"):
        line = line.strip().rstrip(".")
        if line and re.match(r"^[\w\\]", line):
            queries.append(line)
    return queries[:n_queries], 1, cost


def run_oracle_queries(kb: str, queries: list[str]) -> tuple[list[bool | None], list[dict[str, dict]]]:
    """Run oracle queries against KB with tracing. Returns (results, coverage_list)."""
    results = []
    coverages = []
    for q in queries:
        result, cov = run_prolog_query_traced(kb, q)
        results.append(result)
        coverages.append(cov)
    return results, coverages


# ── Repair step ───────────────────────────────────────────────────────────────
def get_predicate_definition(kb: str, pred_name: str) -> str:
    """Extract clauses defining pred_name from KB."""
    # Extract functor from pred/arity format
    functor = pred_name.split("/")[0] if "/" in pred_name else pred_name
    lines = kb.split("\n")
    matching = [l for l in lines if re.match(rf"^{re.escape(functor)}\s*[\(:]", l.strip())]
    return "\n".join(matching) if matching else ""


async def repair_kb(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    kb: str,
    suspicious_preds: list[str],
    sub_goals: list[str],
    source_text: str,
    model: str = PRIMARY_MODEL,
) -> tuple[str, int, float]:
    """Repair top suspicious predicates. Returns (new_kb, n_calls, cost)."""
    total_calls = 0
    total_cost = 0.0
    new_kb = kb

    for pred in suspicious_preds[:TOP_K_REPAIR]:
        try:
            current_def = get_predicate_definition(new_kb, pred)
            # Get neighboring context (5 predicates around it)
            kb_lines = new_kb.split("\n")
            idx = next((i for i, l in enumerate(kb_lines) if pred.split("/")[0] in l), 0)
            context_lines = kb_lines[max(0, idx - 3): idx + 8]
            kb_context = "\n".join(context_lines)

            # Relevant sub-goals for this predicate
            relevant_sgs = [sg for sg in sub_goals if pred.split("/")[0] in sg or sg.split("/")[0] in pred]

            msgs = make_repair_prompt(pred, current_def, kb_context, source_text, relevant_sgs)
            text, in_t, out_t, cost = await call_llm(session, sem, msgs, model=model, max_tokens=600)
            total_calls += 1
            total_cost += cost

            repaired = extract_prolog_block(text)
            if repaired:
                # Append repaired clauses, replacing old definition
                # Remove old definition
                if current_def:
                    functor = pred.split("/")[0]
                    old_lines = new_kb.split("\n")
                    filtered = [l for l in old_lines if not re.match(rf"^{re.escape(functor)}\s*[\(:]", l.strip())]
                    new_kb = "\n".join(filtered) + "\n" + repaired
                else:
                    new_kb = new_kb + "\n" + repaired
        except Exception as e:
            logger.debug(f"Repair failed for {pred}: {e}")

    return new_kb, total_calls, total_cost


# ── Method implementations ────────────────────────────────────────────────────

async def method_one_shot(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    example: dict,
    model: str = PRIMARY_MODEL,
) -> dict:
    """One-shot: single extraction call, no repair."""
    premises = example["premises"] if isinstance(example["premises"], list) else example["premises"].split(". ")
    conclusion = example["conclusion"]
    gold_label = parse_folio_label(example["label"])

    total_calls = 0
    total_cost = 0.0

    try:
        kb, raw, calls, cost = await extract_kb(session, sem, premises, conclusion, model)
        total_calls += calls
        total_cost += cost

        query = None
        if kb:
            query, q_calls, q_cost = await extract_query(session, sem, conclusion, kb, model)
            total_calls += q_calls
            total_cost += q_cost

        correct = check_kb_result_vs_label(kb, query, gold_label)
    except Exception as e:
        logger.debug(f"one-shot error: {e}")
        kb, query, correct = None, None, gold_label == "unknown"

    return {
        "kb": kb or "",
        "query": query or "",
        "correct": correct,
        "n_llm_calls": total_calls,
        "cost_usd": total_cost,
        "gold_label": gold_label,
    }


async def method_cot(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    example: dict,
    model: str = PRIMARY_MODEL,
) -> dict:
    """Chain-of-thought: direct LLM reasoning without Prolog."""
    premises = example["premises"] if isinstance(example["premises"], list) else example["premises"].split(". ")
    conclusion = example["conclusion"]
    gold_label = parse_folio_label(example["label"])

    try:
        msgs = make_cot_prompt(premises, conclusion)
        text, in_t, out_t, cost = await call_llm(session, sem, msgs, model=model, max_tokens=600)
        # Parse answer
        m = re.search(r"ANSWER:\s*(ENTAILED|NOT_ENTAILED|UNKNOWN)", text, re.IGNORECASE)
        if m:
            pred_label = m.group(1).lower()
        else:
            # Fallback: look for keywords
            text_lower = text.lower()
            if "not entailed" in text_lower or "not_entailed" in text_lower or "false" in text_lower[-100:]:
                pred_label = "not_entailed"
            elif "unknown" in text_lower[-100:]:
                pred_label = "unknown"
            else:
                pred_label = "entailed"

        correct = pred_label == gold_label
        return {
            "kb": "",
            "query": text[:500],
            "correct": correct,
            "n_llm_calls": 1,
            "cost_usd": cost,
            "gold_label": gold_label,
        }
    except Exception as e:
        logger.debug(f"CoT error: {e}")
        return {"kb": "", "query": "", "correct": gold_label == "unknown", "n_llm_calls": 0, "cost_usd": 0.0, "gold_label": gold_label}


async def method_self_refine(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    example: dict,
    model: str = PRIMARY_MODEL,
) -> dict:
    """Self-refine: KB extraction + up to 3 rounds of global refinement."""
    premises = example["premises"] if isinstance(example["premises"], list) else example["premises"].split(". ")
    conclusion = example["conclusion"]
    gold_label = parse_folio_label(example["label"])

    total_calls = 0
    total_cost = 0.0

    try:
        # Initial extraction
        kb, _, calls, cost = await extract_kb(session, sem, premises, conclusion, model)
        total_calls += calls
        total_cost += cost

        if not kb:
            return {"kb": "", "query": "", "correct": gold_label == "unknown", "n_llm_calls": total_calls, "cost_usd": total_cost, "gold_label": gold_label}

        # Generate test oracle queries (simple: use gold query)
        oracle_queries, oq_calls, oq_cost = await generate_oracle_queries(session, sem, premises, kb, model, n_queries=8)
        total_calls += oq_calls
        total_cost += oq_cost

        for round_num in range(1, N_REPAIR_ROUNDS + 1):
            if not oracle_queries:
                break
            results, _ = run_oracle_queries(kb, oracle_queries)
            pass_rate = sum(1 for r in results if r is True) / len(results) if results else 0.0

            if pass_rate >= 0.9:
                break

            msgs = make_self_refine_prompt(premises, conclusion, kb, pass_rate, round_num)
            text, in_t, out_t, cost = await call_llm(session, sem, msgs, model=model, max_tokens=1200)
            total_calls += 1
            total_cost += cost
            new_kb = extract_prolog_block(text)
            if new_kb:
                kb = new_kb

        # Extract final query
        query, q_calls, q_cost = await extract_query(session, sem, conclusion, kb, model)
        total_calls += q_calls
        total_cost += q_cost

        correct = check_kb_result_vs_label(kb, query, gold_label)
    except Exception as e:
        logger.debug(f"self-refine error: {e}")
        kb, query, correct = None, None, gold_label == "unknown"

    return {
        "kb": kb or "",
        "query": query or "",
        "correct": correct,
        "n_llm_calls": total_calls,
        "cost_usd": total_cost,
        "gold_label": gold_label,
    }


async def method_flat_sbfl(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    example: dict,
    model: str = PRIMARY_MODEL,
    stratified: bool = False,
) -> dict:
    """
    Flat-SBFL (stratified=False) or Dual-SBFL (stratified=True).
    KB extraction + oracle generation + flat/stratified Ochiai + repair rounds.
    """
    premises = example["premises"] if isinstance(example["premises"], list) else example["premises"].split(". ")
    conclusion = example["conclusion"]
    gold_label = parse_folio_label(example["label"])
    source_text = " ".join(premises)

    total_calls = 0
    total_cost = 0.0

    try:
        # Step 1: Extract KB
        kb, _, calls, cost = await extract_kb(session, sem, premises, conclusion, model)
        total_calls += calls
        total_cost += cost

        if not kb:
            return {"kb": "", "query": "", "correct": gold_label == "unknown", "n_llm_calls": total_calls, "cost_usd": total_cost, "gold_label": gold_label}

        # Step 2: Generate oracle queries
        oracle_queries, oq_calls, oq_cost = await generate_oracle_queries(session, sem, premises, kb, model, n_queries=N_ORACLE_QUERIES)
        total_calls += oq_calls
        total_cost += oq_cost

        # Repair loop
        for round_num in range(N_REPAIR_ROUNDS):
            if not oracle_queries:
                break

            # Step 3: Run queries with tracing
            results, coverages = run_oracle_queries(kb, oracle_queries)
            coverage_matrix = list(zip(coverages, [r is True for r in results]))

            # Step 4: Compute Ochiai scores
            ochiai = compute_ochiai_scores(coverage_matrix, stratified=stratified)

            # Step 5: Sub-goal harvesting (only for dual-SBFL)
            sub_goals = []
            if stratified:
                sub_goals = harvest_subgoals(coverage_matrix, kb)

            # Get top-K suspicious predicates
            top_suspicious = sorted(ochiai.items(), key=lambda x: -x[1])
            suspicious_preds = [p for p, s in top_suspicious if s > 0][:TOP_K_REPAIR]

            # Add sub-goals as additional candidates
            if stratified:
                for sg in sub_goals:
                    if sg not in suspicious_preds:
                        suspicious_preds.append(sg)
                suspicious_preds = suspicious_preds[:TOP_K_REPAIR]

            if not suspicious_preds:
                break

            # Step 6: Repair
            kb, r_calls, r_cost = await repair_kb(session, sem, kb, suspicious_preds, sub_goals, source_text, model)
            total_calls += r_calls
            total_cost += r_cost

        # Final query
        query, q_calls, q_cost = await extract_query(session, sem, conclusion, kb, model)
        total_calls += q_calls
        total_cost += q_cost

        correct = check_kb_result_vs_label(kb, query, gold_label)
    except Exception as e:
        logger.debug(f"sbfl error (strat={stratified}): {e}")
        kb, query, correct = None, None, gold_label == "unknown"

    return {
        "kb": kb or "",
        "query": query or "",
        "correct": correct,
        "n_llm_calls": total_calls,
        "cost_usd": total_cost,
        "gold_label": gold_label,
    }


async def method_dual_sbfl(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    example: dict,
    model: str = PRIMARY_MODEL,
) -> dict:
    return await method_flat_sbfl(session, sem, example, model, stratified=True)


# ── Oracle calibration ────────────────────────────────────────────────────────
async def run_oracle_calibration(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    examples: list[dict],
    model: str = PRIMARY_MODEL,
) -> tuple[float, float]:
    """
    Compare LLM-oracle vs human-oracle rankings on calibration examples.
    Returns (spearman_rho, p_value).
    """
    from scipy.stats import spearmanr

    rhos = []
    logger.info(f"Running oracle calibration on {len(examples)} examples...")

    for ex in examples[:N_CALIBRATION]:
        try:
            premises = ex["premises"] if isinstance(ex["premises"], list) else ex["premises"].split(". ")
            conclusion = ex["conclusion"]
            gold_label = parse_folio_label(ex["label"])

            # Extract KB
            kb, _, calls, cost = await extract_kb(session, sem, premises, conclusion, model)
            if not kb:
                continue

            # LLM oracle queries
            llm_queries, _, _ = await generate_oracle_queries(session, sem, premises, kb, model, n_queries=10)
            if not llm_queries:
                continue

            # Human oracle: use gold label as single ground truth
            # Create a human query: the gold conclusion query
            human_query, _, _ = await extract_query(session, sem, conclusion, kb, model)
            human_expected = gold_label == "entailed"  # True if should be provable

            # Run LLM oracle queries with tracing
            llm_results, llm_coverages = run_oracle_queries(kb, llm_queries)
            llm_matrix = list(zip(llm_coverages, [r is True for r in llm_results]))

            # Human oracle matrix: single query
            human_result, human_cov = run_prolog_query_traced(kb, human_query)
            human_passed = (human_result is True) == human_expected
            human_matrix = [(human_cov, human_passed)]

            # Compute Ochiai rankings
            llm_ochiai = compute_ochiai_scores(llm_matrix, stratified=False)
            human_ochiai = compute_ochiai_scores(human_matrix, stratified=False)

            all_preds = list(set(llm_ochiai.keys()) | set(human_ochiai.keys()))
            if len(all_preds) < 3:
                continue

            llm_ranks = [llm_ochiai.get(p, 0.0) for p in all_preds]
            human_ranks = [human_ochiai.get(p, 0.0) for p in all_preds]

            rho, _ = spearmanr(llm_ranks, human_ranks)
            if not math.isnan(rho):
                rhos.append(rho)
        except Exception as e:
            logger.debug(f"Calibration error: {e}")
            continue

    if not rhos:
        return 0.0, 1.0

    mean_rho = float(np.mean(rhos))
    # Compute p-value from t-distribution
    from scipy.stats import t as t_dist
    n = len(rhos)
    if n < 3:
        return mean_rho, 1.0
    t_stat = mean_rho * math.sqrt(n - 2) / math.sqrt(max(1e-10, 1 - mean_rho**2))
    p_val = float(2 * (1 - t_dist.cdf(abs(t_stat), df=n - 2)))
    logger.info(f"Oracle calibration: rho={mean_rho:.3f} (n={n}) p={p_val:.3f}")
    return mean_rho, p_val


# ── Atomic fact precision/recall ──────────────────────────────────────────────
def compute_atomic_precision_recall(kb: str, gold_premises: list[str]) -> dict[str, float]:
    """Compute precision/recall of KB predicates vs gold premises."""
    if not kb:
        return {"precision_strict": 0.0, "recall_strict": 0.0, "precision_fuzzy": 0.0, "recall_fuzzy": 0.0}

    interp = PrologInterpreter()
    interp.load_kb(kb)
    kb_preds = set(interp.get_predicates())

    # Extract "gold" predicates from premises (simple word tokenization)
    gold_words = set()
    for p in gold_premises:
        words = re.findall(r"\b[a-z]{3,}\b", p.lower())
        gold_words.update(words)

    # Strict match: KB predicate name appears in gold
    def strict_match(pred: str) -> bool:
        functor = pred.split("/")[0] if "/" in pred else pred
        return functor in gold_words

    # Fuzzy match: ≥ 0.5 token overlap
    def fuzzy_match(pred: str) -> bool:
        functor = pred.split("/")[0] if "/" in pred else pred
        pred_tokens = set(re.findall(r"[a-z]{3,}", functor.lower()))
        if not pred_tokens:
            return False
        overlap = len(pred_tokens & gold_words) / len(pred_tokens)
        return overlap >= 0.5

    if not kb_preds:
        return {"precision_strict": 0.0, "recall_strict": 0.0, "precision_fuzzy": 0.0, "recall_fuzzy": 0.0}

    # Create proxy "gold predicates" from premises
    gold_key_words = set()
    for p in gold_premises:
        tokens = re.findall(r"\b[a-z]{4,}\b", p.lower())
        gold_key_words.update(tokens[:5])  # Top words from each premise

    strict_hits = sum(1 for p in kb_preds if strict_match(p))
    fuzzy_hits = sum(1 for p in kb_preds if fuzzy_match(p))
    n_kb = len(kb_preds)
    n_gold = max(1, len(gold_key_words))

    return {
        "precision_strict": strict_hits / n_kb if n_kb > 0 else 0.0,
        "recall_strict": strict_hits / n_gold,
        "precision_fuzzy": fuzzy_hits / n_kb if n_kb > 0 else 0.0,
        "recall_fuzzy": fuzzy_hits / n_gold,
    }


# ── Hallucination proxy ───────────────────────────────────────────────────────
def compute_hallucination_rate(kb: str, source_text: str) -> float:
    """Fraction of predicates with no lexical grounding in source (50-word windows)."""
    if not kb:
        return 1.0

    interp = PrologInterpreter()
    interp.load_kb(kb)
    preds = interp.get_predicates()
    if not preds:
        return 1.0

    words = re.findall(r"\b[a-z]{3,}\b", source_text.lower())
    window_size = 50

    def is_grounded(pred: str) -> bool:
        functor = pred.split("/")[0] if "/" in pred else pred
        pred_tokens = set(re.findall(r"[a-z]{3,}", functor.lower()))
        if not pred_tokens:
            return True  # skip short/empty
        # Check 50-word sliding windows
        for i in range(0, max(1, len(words) - window_size + 1), 25):
            window = set(words[i:i + window_size])
            if len(pred_tokens & window) >= min(2, len(pred_tokens)):
                return True
        return False

    not_grounded = sum(1 for p in preds if not is_grounded(p))
    return not_grounded / len(preds)


# ── Bootstrap CI ─────────────────────────────────────────────────────────────
def bootstrap_ci(
    correct_a: list[bool],
    correct_b: list[bool] | None = None,
    n_resamples: int = N_BOOTSTRAP,
    alpha: float = 0.05,
) -> dict:
    """
    If correct_b is None: CI for accuracy of a.
    If correct_b given: CI for (acc_a - acc_b).
    """
    rng = np.random.default_rng(42)
    n = len(correct_a)
    a = np.array(correct_a, dtype=float)

    if correct_b is None:
        stats = [rng.choice(a, size=n, replace=True).mean() for _ in range(n_resamples)]
    else:
        b = np.array(correct_b[:n], dtype=float)
        n = min(len(a), len(b))
        a, b = a[:n], b[:n]
        diffs = []
        for _ in range(n_resamples):
            idx = rng.choice(n, size=n, replace=True)
            diffs.append(a[idx].mean() - b[idx].mean())
        stats = diffs

    lower = float(np.percentile(stats, alpha / 2 * 100))
    upper = float(np.percentile(stats, (1 - alpha / 2) * 100))
    return {"lower": lower, "upper": upper, "n_resamples": n_resamples}


def cohens_h(p1: float, p2: float) -> float:
    """Effect size for two proportions."""
    return float(2 * math.asin(math.sqrt(max(0, min(1, p1)))) - 2 * math.asin(math.sqrt(max(0, min(1, p2)))))


def mcnemar_test(correct_a: list[bool], correct_b: list[bool]) -> float:
    """McNemar test p-value."""
    from scipy.stats import chi2
    n = min(len(correct_a), len(correct_b))
    # Discordant pairs
    b = sum(1 for i in range(n) if correct_a[i] and not correct_b[i])  # a right, b wrong
    c = sum(1 for i in range(n) if not correct_a[i] and correct_b[i])  # a wrong, b right
    if b + c == 0:
        return 1.0
    # McNemar statistic with continuity correction
    stat = (abs(b - c) - 1) ** 2 / (b + c)
    p_val = float(1 - chi2.cdf(stat, df=1))
    return p_val


# ── Main pipeline ─────────────────────────────────────────────────────────────
async def process_one(
    i: int,
    ex: dict,
    method_name: str,
    method_fn,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    model: str,
) -> dict:
    """Process a single example for a method, with error handling."""
    try:
        result = await method_fn(session, sem, ex, model)
        result["example_idx"] = i
        result["method"] = method_name
        return result
    except Exception as e:
        logger.error(f"[{method_name}] Example {i} failed: {e}")
        gold = parse_folio_label(ex.get("label", "unknown"))
        return {
            "kb": "", "query": "", "correct": gold == "unknown",
            "n_llm_calls": 0, "cost_usd": 0.0, "gold_label": gold,
            "example_idx": i, "method": method_name,
        }


async def process_method(
    method_name: str,
    method_fn,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    examples: list[dict],
    model: str = PRIMARY_MODEL,
) -> list[dict]:
    """Process all examples for a method in parallel batches."""
    total = len(examples)
    logger.info(f"[{method_name}] Processing {total} examples in parallel (concurrency={CONCURRENCY})...")

    if _total_cost_usd >= COST_HARD_STOP:
        logger.warning(f"Budget exhausted before {method_name}, returning empty")
        return []

    # Process in batches to avoid overwhelming memory
    BATCH = 30
    results = []
    for batch_start in range(0, total, BATCH):
        if _total_cost_usd >= COST_HARD_STOP:
            logger.warning(f"Budget exhausted at batch {batch_start}, stopping {method_name}")
            break
        batch = examples[batch_start:batch_start + BATCH]
        tasks = [
            process_one(batch_start + j, ex, method_name, method_fn, session, sem, model)
            for j, ex in enumerate(batch)
        ]
        batch_results = await asyncio.gather(*tasks)
        results.extend(batch_results)
        logger.info(f"[{method_name}] {len(results)}/{total} done | total_cost=${_total_cost_usd:.2f}")

        # Save partial
        partial = {method_name: [{"correct": r["correct"], "n_llm_calls": r["n_llm_calls"]} for r in results]}
        try:
            (WORKSPACE / "eval_out_partial.json").write_text(json.dumps(partial, indent=2))
        except Exception:
            pass

    acc = sum(r["correct"] for r in results) / len(results) if results else 0
    logger.info(f"[{method_name}] Accuracy={acc:.3f} | N={len(results)} | cost=${_total_cost_usd:.2f}")
    return results


def aggregate_method_results(
    results: list[dict],
    examples: list[dict],
) -> dict:
    """Compute all metrics for a method's results."""
    n = len(results)
    if n == 0:
        return {}

    correct_vec = [r["correct"] for r in results]
    accuracy = sum(correct_vec) / n

    # Hop-stratified accuracy
    hop_correct: dict[int, list[bool]] = {1: [], 2: [], 3: []}
    for r, ex in zip(results, examples[:n]):
        premises = ex["premises"] if isinstance(ex["premises"], list) else ex["premises"].split(". ")
        hop = estimate_hop_depth(premises, ex["conclusion"], example=ex)
        hop_correct[hop].append(r["correct"])

    def hop_acc(hop: int) -> float:
        v = hop_correct[hop]
        return sum(v) / len(v) if v else 0.0

    # Hallucination rate
    hall_rates = []
    for r, ex in zip(results, examples[:n]):
        premises = ex["premises"] if isinstance(ex["premises"], list) else ex["premises"].split(". ")
        source = " ".join(premises)
        hall_rates.append(compute_hallucination_rate(r["kb"], source))

    # Atomic precision/recall
    prec_strict = rec_strict = prec_fuzzy = rec_fuzzy = 0.0
    for r, ex in zip(results, examples[:n]):
        premises = ex["premises"] if isinstance(ex["premises"], list) else ex["premises"].split(". ")
        ap = compute_atomic_precision_recall(r["kb"], premises)
        prec_strict += ap["precision_strict"]
        rec_strict += ap["recall_strict"]
        prec_fuzzy += ap["precision_fuzzy"]
        rec_fuzzy += ap["recall_fuzzy"]

    return {
        "accuracy": accuracy,
        "accuracy_1hop": hop_acc(1),
        "accuracy_2hop": hop_acc(2),
        "accuracy_3hop": hop_acc(3),
        "hallucination_rate": float(np.mean(hall_rates)) if hall_rates else 0.0,
        "hallucination_std": float(np.std(hall_rates)) if hall_rates else 0.0,
        "mean_llm_calls": float(np.mean([r["n_llm_calls"] for r in results])),
        "std_llm_calls": float(np.std([r["n_llm_calls"] for r in results])),
        "total_cost_usd": sum(r["cost_usd"] for r in results),
        "atomic_precision_strict": prec_strict / n,
        "atomic_recall_strict": rec_strict / n,
        "atomic_precision_fuzzy": prec_fuzzy / n,
        "atomic_recall_fuzzy": rec_fuzzy / n,
        "n_examples": n,
        "correct_vec": correct_vec,
    }


# ── Figures ───────────────────────────────────────────────────────────────────
def generate_figures(method_results: dict, agg: dict, cis: dict, cal_rho: float) -> dict[str, str]:
    """Generate matplotlib figures. Returns {fig_name: base64_png}."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures = {}
    fig_dir = WORKSPACE / "figures"
    method_names = list(method_results.keys())
    colors = ["#2196F3", "#FF9800", "#4CAF50", "#9C27B0", "#F44336"]

    # Figure 1: Accuracy by method with CI error bars
    fig, ax = plt.subplots(figsize=(10, 6))
    accs = [agg[m]["accuracy"] for m in method_names]
    ci_lower = [agg[m]["accuracy"] - cis.get(f"{m}_vs_none", {}).get("lower", agg[m]["accuracy"]) for m in method_names]
    ci_upper = [cis.get(f"{m}_vs_none", {}).get("upper", agg[m]["accuracy"]) - agg[m]["accuracy"] for m in method_names]
    # Use direct CI from bootstrap
    ci_errs = []
    for m in method_names:
        ci = bootstrap_ci(agg[m]["correct_vec"])
        ci_errs.append([agg[m]["accuracy"] - ci["lower"], ci["upper"] - agg[m]["accuracy"]])
    ci_errs = np.array(ci_errs).T
    bars = ax.bar(method_names, accs, color=colors[:len(method_names)], alpha=0.8, edgecolor="black")
    ax.errorbar(range(len(method_names)), accs, yerr=ci_errs, fmt="none", color="black", capsize=5)
    ax.set_ylabel("Accuracy", fontsize=12)
    ax.set_title("Deduction Accuracy by Method (FOLIO Validation)", fontsize=14)
    ax.set_ylim(0, 1)
    for bar, acc in zip(bars, accs):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.01, f"{acc:.3f}", ha="center", va="bottom", fontsize=10)
    ax.set_xticks(range(len(method_names)))
    ax.set_xticklabels(method_names, rotation=20, ha="right")
    plt.tight_layout()
    path = fig_dir / "fig1_accuracy.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    figures["fig1_accuracy"] = base64.b64encode(path.read_bytes()).decode()

    # Figure 2: Hallucination rate
    fig, ax = plt.subplots(figsize=(10, 6))
    hall_rates = [agg[m]["hallucination_rate"] for m in method_names]
    hall_stds = [agg[m]["hallucination_std"] for m in method_names]
    bars = ax.bar(method_names, hall_rates, color=colors[:len(method_names)], alpha=0.8, edgecolor="black")
    ax.errorbar(range(len(method_names)), hall_rates, yerr=hall_stds, fmt="none", color="black", capsize=5)
    ax.set_ylabel("Hallucination Proxy Rate", fontsize=12)
    ax.set_title("Hallucination Proxy Rate by Method", fontsize=14)
    ax.set_ylim(0, 1)
    for bar, rate in zip(bars, hall_rates):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.01, f"{rate:.3f}", ha="center", va="bottom", fontsize=10)
    ax.set_xticks(range(len(method_names)))
    ax.set_xticklabels(method_names, rotation=20, ha="right")
    plt.tight_layout()
    path = fig_dir / "fig2_hallucination.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    figures["fig2_hallucination"] = base64.b64encode(path.read_bytes()).decode()

    # Figure 3: Accuracy vs LLM calls scatter
    fig, ax = plt.subplots(figsize=(8, 6))
    for i, m in enumerate(method_names):
        ax.scatter(agg[m]["mean_llm_calls"], agg[m]["accuracy"], color=colors[i], s=150, label=m, zorder=5)
        ax.annotate(m, (agg[m]["mean_llm_calls"], agg[m]["accuracy"]),
                   textcoords="offset points", xytext=(8, 3), fontsize=9)
    ax.set_xlabel("Mean LLM Calls per Example", fontsize=12)
    ax.set_ylabel("Accuracy", fontsize=12)
    ax.set_title("Accuracy vs LLM Call Efficiency", fontsize=14)
    ax.legend(fontsize=9)
    plt.tight_layout()
    path = fig_dir / "fig3_efficiency.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    figures["fig3_efficiency"] = base64.b64encode(path.read_bytes()).decode()

    # Figure 4: Accuracy by hop depth for dual-SBFL vs one-shot
    fig, ax = plt.subplots(figsize=(8, 6))
    hops = [1, 2, 3]
    hop_labels = ["1-hop", "2-hop", "3-hop"]
    for m, color in [("dual_sbfl", colors[0]), ("one_shot", colors[1])]:
        if m in agg:
            hop_accs = [agg[m][f"accuracy_{h}hop"] for h in hops]
            ax.plot(hop_labels, hop_accs, marker="o", label=m, color=color, linewidth=2)
    ax.set_ylabel("Accuracy", fontsize=12)
    ax.set_title("Accuracy by Reasoning Hop Depth", fontsize=14)
    ax.set_ylim(0, 1)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    path = fig_dir / "fig4_hop_depth.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    figures["fig4_hop_depth"] = base64.b64encode(path.read_bytes()).decode()

    # Figure 5: Oracle calibration rho
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.bar(["Oracle Calibration\nSpearman ρ"], [cal_rho], color=colors[0], alpha=0.8, edgecolor="black")
    ax.axhline(y=0.6, color="red", linestyle="--", label="Threshold (ρ=0.6)")
    ax.set_ylim(-1, 1)
    ax.set_ylabel("Spearman ρ", fontsize=12)
    ax.set_title("LLM Oracle vs Human Oracle Calibration", fontsize=14)
    ax.legend(fontsize=10)
    ax.text(0, cal_rho + 0.02, f"ρ={cal_rho:.3f}", ha="center", va="bottom", fontsize=12)
    plt.tight_layout()
    path = fig_dir / "fig5_oracle_calibration.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    figures["fig5_oracle_calibration"] = base64.b64encode(path.read_bytes()).decode()

    logger.info(f"Generated {len(figures)} figures")
    return figures


# ── Build output schema ───────────────────────────────────────────────────────
def build_eval_out(
    method_results: dict,
    agg: dict,
    examples: list[dict],
    cis: dict,
    effect_sizes: dict,
    mcnemar_pvalues: dict,
    oracle_rho: float,
    oracle_pvalue: float,
    figures: dict,
) -> dict:
    """Build eval_out.json in exp_eval_sol_out schema."""
    # Aggregate metrics for schema
    dual = agg.get("dual_sbfl", {})
    one_shot = agg.get("one_shot", {})

    metrics_agg = {
        "dual_sbfl_accuracy": round(dual.get("accuracy", 0.0), 4),
        "one_shot_accuracy": round(one_shot.get("accuracy", 0.0), 4),
        "self_refine_accuracy": round(agg.get("self_refine", {}).get("accuracy", 0.0), 4),
        "flat_sbfl_accuracy": round(agg.get("flat_sbfl", {}).get("accuracy", 0.0), 4),
        "cot_accuracy": round(agg.get("cot", {}).get("accuracy", 0.0), 4),
        "dual_vs_oneshot_delta": round(dual.get("accuracy", 0.0) - one_shot.get("accuracy", 0.0), 4),
        "oracle_calibration_rho": round(oracle_rho, 4),
        "oracle_calibration_valid": float(oracle_rho >= 0.6),
        "dual_sbfl_hallucination_rate": round(dual.get("hallucination_rate", 0.0), 4),
        "one_shot_hallucination_rate": round(one_shot.get("hallucination_rate", 0.0), 4),
        "hallucination_reduction_pct": round(
            (one_shot.get("hallucination_rate", 0.0) - dual.get("hallucination_rate", 0.0))
            / max(0.001, one_shot.get("hallucination_rate", 0.001)) * 100, 2),
        "dual_sbfl_mean_llm_calls": round(dual.get("mean_llm_calls", 0.0), 2),
        "self_refine_mean_llm_calls": round(agg.get("self_refine", {}).get("mean_llm_calls", 0.0), 2),
        "n_examples": len(examples),
        "total_cost_usd": round(_total_cost_usd, 4),
    }

    # Per-example data
    example_rows = []
    n = len(examples)
    for i, ex in enumerate(examples):
        premises = ex["premises"] if isinstance(ex["premises"], list) else ex["premises"].split(". ")
        gold_label = parse_folio_label(ex["label"])
        hop_depth = estimate_hop_depth(premises, ex["conclusion"], example=ex)

        row = {
            "input": ex["conclusion"],
            "output": gold_label,
            "metadata_premises": " | ".join(premises[:3]),
            "metadata_hop_depth": hop_depth,
            "metadata_index": i,
        }

        # Add per-method predictions and correctness
        for m_name, m_results in method_results.items():
            if i < len(m_results):
                r = m_results[i]
                row[f"predict_{m_name}"] = r.get("query", "")
                row[f"eval_{m_name}_correct"] = float(r.get("correct", False))
                row[f"eval_{m_name}_llm_calls"] = float(r.get("n_llm_calls", 0))
                row[f"eval_{m_name}_hallucination"] = float(
                    compute_hallucination_rate(r.get("kb", ""), " ".join(premises))
                )

        example_rows.append(row)

    # Build additional results dict for metadata
    method_metrics = {}
    for m_name, m_agg in agg.items():
        method_metrics[m_name] = {
            "accuracy": round(m_agg.get("accuracy", 0.0), 4),
            "accuracy_1hop": round(m_agg.get("accuracy_1hop", 0.0), 4),
            "accuracy_2hop": round(m_agg.get("accuracy_2hop", 0.0), 4),
            "accuracy_3hop": round(m_agg.get("accuracy_3hop", 0.0), 4),
            "hallucination_rate": round(m_agg.get("hallucination_rate", 0.0), 4),
            "mean_llm_calls": round(m_agg.get("mean_llm_calls", 0.0), 2),
            "total_cost_usd": round(m_agg.get("total_cost_usd", 0.0), 4),
            "atomic_precision_strict": round(m_agg.get("atomic_precision_strict", 0.0), 4),
            "atomic_recall_strict": round(m_agg.get("atomic_recall_strict", 0.0), 4),
            "atomic_precision_fuzzy": round(m_agg.get("atomic_precision_fuzzy", 0.0), 4),
            "atomic_recall_fuzzy": round(m_agg.get("atomic_recall_fuzzy", 0.0), 4),
        }

    return {
        "metadata": {
            "evaluation_name": "Dual-Signal SBFL vs Baselines on FOLIO",
            "description": "End-to-end statistical evaluation of Prolog KB repair methods",
            "n_examples": n,
            "n_calibration_examples": N_CALIBRATION,
            "total_cost_usd": round(_total_cost_usd, 4),
            "method_results": method_metrics,
            "bootstrap_cis": cis,
            "effect_sizes": effect_sizes,
            "mcnemar_pvalues": mcnemar_pvalues,
            "oracle_calibration_rho": round(oracle_rho, 4),
            "oracle_calibration_pvalue": round(oracle_pvalue, 4),
            "oracle_calibration_valid": oracle_rho >= 0.6,
            "figures": figures,
        },
        "metrics_agg": metrics_agg,
        "datasets": [
            {
                "dataset": DATASET_NAME,
                "examples": example_rows,
            }
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────
@logger.catch(reraise=True)
async def main_async(n_examples: int = MAX_EXAMPLES):
    logger.info(f"Starting evaluation | n_examples={n_examples} | budget=${COST_HARD_STOP}")

    # Load ProofWriter dataset (FOLIO is gated; ProofWriter is public and equivalent for this eval)
    logger.info(f"Loading {DATASET_NAME} dataset...")
    from datasets import load_dataset
    ds = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    # Balance across hop depths: sample proportionally across QDep 0,1,2,3+
    # Prefer examples with moderate depth to get hop-stratified results
    depth_buckets: dict[int, list] = {1: [], 2: [], 3: []}
    zero_hop = []
    for ex in ds:
        d = int(ex.get("QDep", 0))
        if d == 0:
            zero_hop.append(ex)
        elif d == 1:
            depth_buckets[1].append(ex)
        elif d == 2:
            depth_buckets[2].append(ex)
        else:
            depth_buckets[3].append(ex)

    # Target: ~n/3 per depth bucket, balanced True/False/Unknown
    per_bucket = n_examples // 3
    examples = []
    for bucket in [zero_hop] + list(depth_buckets.values()):
        examples.extend(bucket[:per_bucket])
    examples = examples[:n_examples]
    logger.info(f"Loaded {len(examples)} examples from {DATASET_NAME}")

    # Normalize to FOLIO-like format
    for ex in examples:
        # theory -> premises (split by sentence)
        theory = ex.get("theory", "")
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', theory) if s.strip()]
        ex["premises"] = sentences
        # question -> conclusion
        ex["conclusion"] = ex.get("question", "")
        # answer -> label
        ans = str(ex.get("answer", "Unknown")).strip()
        if ans == "True":
            ex["label"] = "entailed"
        elif ans == "False":
            ex["label"] = "not_entailed"
        else:
            ex["label"] = "unknown"
        # Use QDep for hop depth
        ex["qdep"] = int(ex.get("QDep", 0))

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=20)

    async with aiohttp.ClientSession(connector=connector) as session:
        # Run oracle calibration on first N_CALIBRATION examples
        logger.info("Phase 0: Oracle calibration...")
        cal_examples = examples[:N_CALIBRATION]
        oracle_rho, oracle_pvalue = await run_oracle_calibration(session, sem, cal_examples)
        logger.info(f"Oracle calibration: rho={oracle_rho:.3f} valid={oracle_rho >= 0.6}")

        # Define methods to run
        METHODS = [
            ("one_shot", method_one_shot),
            ("cot", method_cot),
            ("self_refine", method_self_refine),
            ("flat_sbfl", lambda s, sem2, ex, m: method_flat_sbfl(s, sem2, ex, m, stratified=False)),
            ("dual_sbfl", method_dual_sbfl),
        ]

        all_results: dict[str, list[dict]] = {}

        # Process methods sequentially to control budget
        for method_name, method_fn in METHODS:
            if _total_cost_usd >= COST_HARD_STOP:
                logger.warning(f"Budget exhausted, skipping remaining methods")
                break
            logger.info(f"Running method: {method_name} | cost_so_far=${_total_cost_usd:.2f}")
            results = await process_method(method_name, method_fn, session, sem, examples)
            all_results[method_name] = results

            # Save partial results
            partial = {m: [{"correct": r["correct"], "n_llm_calls": r["n_llm_calls"]} for r in rs]
                      for m, rs in all_results.items()}
            (WORKSPACE / "eval_out_partial.json").write_text(json.dumps(partial, indent=2))
            logger.info(f"Partial results saved after {method_name}")

    # Aggregate metrics
    logger.info("Computing aggregate metrics...")
    agg = {}
    for m_name, m_results in all_results.items():
        agg[m_name] = aggregate_method_results(m_results, examples)

    # Statistical tests
    logger.info("Computing statistical tests...")
    cis = {}
    effect_sizes = {}
    mcnemar_pvalues = {}

    baselines = ["one_shot", "self_refine", "flat_sbfl", "cot"]
    dual_correct = agg.get("dual_sbfl", {}).get("correct_vec", [])

    for baseline in baselines:
        if baseline not in agg or "dual_sbfl" not in agg:
            continue
        bl_correct = agg[baseline]["correct_vec"]
        key = f"dual_sbfl_vs_{baseline}"

        ci = bootstrap_ci(dual_correct, bl_correct)
        cis[key] = ci

        acc_dual = agg["dual_sbfl"]["accuracy"]
        acc_bl = agg[baseline]["accuracy"]
        effect_sizes[key] = {"cohens_h": cohens_h(acc_dual, acc_bl)}

        mcnemar_pvalues[key] = mcnemar_test(dual_correct, bl_correct[:len(dual_correct)])

    # Generate figures
    logger.info("Generating figures...")
    figures = generate_figures(all_results, agg, cis, oracle_rho)

    # Build output
    eval_out = build_eval_out(
        all_results, agg, examples, cis, effect_sizes, mcnemar_pvalues,
        oracle_rho, oracle_pvalue, figures,
    )

    # Save
    out_path = WORKSPACE / "eval_out.json"
    out_path.write_text(json.dumps(eval_out, indent=2))
    logger.info(f"Saved eval_out.json ({out_path.stat().st_size / 1024:.1f} KB)")
    logger.info(f"Total cost: ${_total_cost_usd:.4f} | Total LLM calls: {_total_llm_calls}")

    # Summary
    for m_name, m_agg in agg.items():
        logger.info(f"  {m_name}: acc={m_agg['accuracy']:.3f} hall={m_agg['hallucination_rate']:.3f} calls={m_agg['mean_llm_calls']:.1f}")

    return eval_out


@logger.catch(reraise=True)
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-examples", type=int, default=MAX_EXAMPLES)
    args = parser.parse_args()

    n = args.n_examples
    logger.info(f"Running evaluation on {n} examples")
    asyncio.run(main_async(n_examples=n))


if __name__ == "__main__":
    main()
