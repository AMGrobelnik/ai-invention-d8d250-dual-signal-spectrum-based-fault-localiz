"""Quick smoke test: 2 ProofWriter examples."""
import json, sys, os
sys.path.insert(0, ".")
os.environ.setdefault("OPENROUTER_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))

from pathlib import Path
import method

examples = json.loads(Path("/ai-inventor/aii_data/runs/run_ITc1Qruy7fap/3_invention_loop/iter_1/gen_art/gen_art_dataset_1/mini_data_out.json").read_text())
pw = next(ds for ds in examples["datasets"] if "proofwriter" in ds["dataset"])
exs = pw["examples"][:2]

print("=== Test KB extraction ===")
clauses = method.extract_kb(exs[0]["metadata_premises_text"])
print(f"Extracted {len(clauses)} clauses:")
for c in clauses[:5]:
    print(" ", c)

print("\n=== Test oracle queries ===")
queries = method.generate_oracle_queries(exs[0]["metadata_premises_text"], exs[0]["metadata_conclusion"], n_queries=4)
print(f"Generated {len(queries)} queries:")
for q in queries[:3]:
    print(" ", q)

print("\n=== Test Prolog ===")
prolog = method.build_prolog_instance(clauses)
print(f"Prolog instance: {prolog is not None}")

print("\n=== Test baseline ===")
pred = method.direct_classify(exs[0]["metadata_premises_text"], exs[0]["metadata_conclusion"])
print(f"Baseline pred: {pred}, gold: {exs[0]['output']}")

print("\n=== Test SBFL pipeline (1 example) ===")
ochiai, missing, qr = method.run_sbfl_pipeline(clauses, queries, exs[0]["metadata_premises_text"])
print(f"Ochiai scores: {dict(list(ochiai.items())[:5])}")
print(f"Missing predicates: {dict(list(missing.items())[:5])}")

print(f"\nTotal cost so far: ${method.cumulative_cost_usd:.4f}")
print("Smoke test PASSED")
