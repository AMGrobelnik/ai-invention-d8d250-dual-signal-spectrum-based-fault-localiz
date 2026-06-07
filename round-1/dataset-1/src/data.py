#!/usr/bin/env python3
"""Load FOLIO-KR and ProofWriter-NL (RuleTaker depth-3) and format to exp_sel_data_out schema."""

# /// script
# requires-python = ">=3.12"
# dependencies = ["loguru"]
# ///

import json
import sys
from pathlib import Path
from loguru import logger

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

WORKSPACE = Path("/ai-inventor/aii_data/runs/run_ITc1Qruy7fap/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")
TEMP = WORKSPACE / "temp" / "datasets"

FOLIO_LABEL_MAP = {"True": "True", "False": "False", "Uncertain": "Unknown"}
RULETAKER_LABEL_MAP = {"entailment": "True", "not entailment": "False"}


def load_folio_examples() -> list[dict]:
    examples = []
    splits = [
        ("train", "train"),
        ("validation", "val"),
        ("test", "test"),
    ]
    for hf_split, out_split in splits:
        path = TEMP / f"full_HannaAbiAkl_FOLIO-KR_default_{hf_split}.json"
        data = json.loads(path.read_text())
        logger.info(f"FOLIO-KR {hf_split}: {len(data)} rows")
        for idx, ex in enumerate(data):
            premises_raw = ex.get("premises", "") or ""
            premises_lines = [p.strip() for p in premises_raw.strip().split("\n") if p.strip()]
            premises_text = " ".join(premises_lines)[:3000]

            conclusion = ex.get("conclusion", "").strip()
            gold_label = FOLIO_LABEL_MAP.get(ex.get("label", ""), "Unknown")

            fol_premises_raw = ex.get("premises-FOL", "") or ""
            fol_premises = [l.strip() for l in fol_premises_raw.split("\n") if l.strip()]
            fol_conclusion = (ex.get("conclusion-FOL", "") or "").strip()

            is_calibration = (out_split == "val" and idx < 20)
            actual_split = "calibration" if is_calibration else out_split

            input_text = f"Premises: {premises_text}\nConclusion: {conclusion}"

            examples.append({
                "input": input_text,
                "output": gold_label,
                "metadata_fold": out_split,
                "metadata_split": actual_split,
                "metadata_is_calibration_doc": is_calibration,
                "metadata_gold_label": gold_label,
                "metadata_premises_text": premises_text,
                "metadata_conclusion": conclusion,
                "metadata_gold_fol_premises": json.dumps(fol_premises),
                "metadata_gold_fol_conclusion": fol_conclusion,
                "metadata_story_id": str(ex.get("story_id", "")),
                "metadata_example_id": str(ex.get("example_id", "")),
                "metadata_task_type": "classification",
                "metadata_fold_name": "folio",
            })
    return examples


def load_proofwriter_examples(max_examples: int = 300) -> list[dict]:
    import random
    random.seed(42)

    path = TEMP / "full_tasksource_ruletaker_default_train.json"
    data = json.loads(path.read_text())

    d3nl = [x for x in data if x.get("config") == "depth-3ext-NatLang"]
    if not d3nl:
        d3nl = [x for x in data if x.get("config") == "depth-3"]
    logger.info(f"RuleTaker depth-3ext-NatLang candidates: {len(d3nl)}")

    true_ex = [x for x in d3nl if x.get("label") == "entailment"]
    false_ex = [x for x in d3nl if x.get("label") == "not entailment"]
    per_class = max_examples // 2
    selected = random.sample(true_ex, min(per_class, len(true_ex))) + \
               random.sample(false_ex, min(per_class, len(false_ex)))
    selected = selected[:max_examples]
    logger.info(f"Selected {len(selected)} ruletaker examples (balanced)")

    examples = []
    for idx, ex in enumerate(selected):
        context = ex.get("context", "").strip()[:3000]
        question = ex.get("question", "").strip()
        gold_label = RULETAKER_LABEL_MAP.get(ex.get("label", ""), "Unknown")

        input_text = f"Premises: {context}\nConclusion: {question}"
        examples.append({
            "input": input_text,
            "output": gold_label,
            "metadata_fold": "test",
            "metadata_split": "test",
            "metadata_is_calibration_doc": False,
            "metadata_gold_label": gold_label,
            "metadata_premises_text": context,
            "metadata_conclusion": question,
            "metadata_gold_fol_premises": "[]",
            "metadata_gold_fol_conclusion": "",
            "metadata_story_id": "",
            "metadata_example_id": str(idx),
            "metadata_task_type": "classification",
            "metadata_fold_name": "proofwriter",
        })
    return examples


@logger.catch(reraise=True)
def main() -> None:
    logger.info("Building full_data_out.json")

    folio_examples = load_folio_examples()
    logger.info(f"FOLIO examples: {len(folio_examples)}")

    pw_examples = load_proofwriter_examples(max_examples=300)
    logger.info(f"ProofWriter examples: {len(pw_examples)}")

    output = {
        "metadata": {
            "description": "FOLIO + ProofWriter-NL dataset for Dual-Signal SBFL evaluation",
            "folio_source": "HannaAbiAkl/FOLIO-KR (mirrors yale-nlp/FOLIO with additional KR notations)",
            "proofwriter_source": "tasksource/ruletaker config=depth-3ext-NatLang",
            "total_examples": len(folio_examples) + len(pw_examples),
        },
        "datasets": [
            {
                "dataset": "folio",
                "examples": folio_examples,
            },
            {
                "dataset": "proofwriter_ruletaker_depth3",
                "examples": pw_examples,
            },
        ],
    }

    out_path = WORKSPACE / "full_data_out.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    logger.info(f"Saved full_data_out.json: {len(folio_examples) + len(pw_examples)} total examples ({out_path.stat().st_size / 1e6:.1f} MB)")
    logger.info("Done.")


if __name__ == "__main__":
    main()
