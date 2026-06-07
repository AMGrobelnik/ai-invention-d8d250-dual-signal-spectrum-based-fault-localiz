#!/usr/bin/env python3
"""Build unified FOLIO + ProofWriter-NL dataset for Dual-Signal SBFL evaluation."""

import json
import sys
import random
import collections
from pathlib import Path
from loguru import logger

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

WORKSPACE = Path("/ai-inventor/aii_data/runs/run_ITc1Qruy7fap/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")
TEMP = WORKSPACE / "temp" / "datasets"

FOLIO_LABEL_MAP = {"True": "True", "False": "False", "Uncertain": "Unknown"}
RULETAKER_LABEL_MAP = {"entailment": "True", "not entailment": "False"}


def load_folio() -> list[dict]:
    rows = []
    splits = [
        ("train", "train"),
        ("validation", "val"),
        ("test", "test"),
    ]
    for hf_split, out_split in splits:
        path = TEMP / f"full_HannaAbiAkl_FOLIO-KR_default_{hf_split}.json"
        data = json.loads(path.read_text())
        logger.info(f"FOLIO {hf_split}: {len(data)} rows")
        for idx, ex in enumerate(data):
            premises_list = ex["premises"].strip().split("\n") if ex["premises"] else []
            premises_text = " ".join(p.strip() for p in premises_list if p.strip())
            premises_text = premises_text[:3000]

            fol_premises_raw = ex.get("premises-FOL", "")
            fol_premises = [
                line.strip()
                for line in (fol_premises_raw or "").split("\n")
                if line.strip()
            ]
            fol_conclusion = ex.get("conclusion-FOL", "") or ""

            gold_label = FOLIO_LABEL_MAP.get(ex.get("label", ""), "Unknown")
            row = {
                "id": f"folio_{out_split}_{idx}",
                "premises_text": premises_text,
                "conclusion": ex.get("conclusion", "").strip(),
                "gold_label": gold_label,
                "gold_fol_premises": fol_premises,
                "gold_fol_conclusion": fol_conclusion.strip(),
                "split": out_split,
                "is_calibration_doc": False,
                "metadata_fold": "folio",
            }
            rows.append(row)
    return rows


def assign_calibration(rows: list[dict]) -> list[dict]:
    val_rows = [r for r in rows if r["split"] == "val"]
    val_rows.sort(key=lambda r: int(r["id"].split("_")[-1]))
    calibration_ids = {r["id"] for r in val_rows[:20]}
    for r in rows:
        if r["id"] in calibration_ids:
            r["split"] = "calibration"
            r["is_calibration_doc"] = True
    logger.info(f"Assigned {len(calibration_ids)} calibration docs")
    return rows


def load_ruletaker_depth3(max_examples: int = 300) -> list[dict]:
    path = TEMP / "full_tasksource_ruletaker_default_train.json"
    data = json.loads(path.read_text())

    # Prefer NatLang variant for NL surface forms
    d3nl = [x for x in data if x.get("config") == "depth-3ext-NatLang"]
    if not d3nl:
        d3nl = [x for x in data if x.get("config") == "depth-3"]
    logger.info(f"RuleTaker depth-3 NatLang candidates: {len(d3nl)}")

    # Balance True/False
    true_ex = [x for x in d3nl if x.get("label") == "entailment"]
    false_ex = [x for x in d3nl if x.get("label") == "not entailment"]
    per_class = max_examples // 2
    random.seed(42)
    selected = random.sample(true_ex, min(per_class, len(true_ex))) + \
               random.sample(false_ex, min(per_class, len(false_ex)))
    selected = selected[:max_examples]
    logger.info(f"Selected {len(selected)} ruletaker examples (balanced)")

    rows = []
    for idx, ex in enumerate(selected):
        premises_text = ex.get("context", "").strip()[:3000]
        gold_label = RULETAKER_LABEL_MAP.get(ex.get("label", ""), "Unknown")
        row = {
            "id": f"proofwriter_{idx}",
            "premises_text": premises_text,
            "conclusion": ex.get("question", "").strip(),
            "gold_label": gold_label,
            "gold_fol_premises": [],
            "gold_fol_conclusion": "",
            "split": "test",
            "is_calibration_doc": False,
            "metadata_fold": "proofwriter",
        }
        rows.append(row)
    return rows


def validate(rows: list[dict]) -> None:
    required_keys = {"id", "premises_text", "conclusion", "gold_label",
                     "gold_fol_premises", "gold_fol_conclusion", "split",
                     "is_calibration_doc", "metadata_fold"}
    valid_labels = {"True", "False", "Unknown"}

    errors = []
    for i, r in enumerate(rows):
        missing = required_keys - set(r.keys())
        if missing:
            errors.append(f"Row {i} missing keys: {missing}")
        if not r.get("premises_text", "").strip():
            errors.append(f"Row {i} empty premises_text")
        if r.get("gold_label") not in valid_labels:
            errors.append(f"Row {i} invalid gold_label: {r.get('gold_label')}")

    calibration_count = sum(1 for r in rows if r.get("is_calibration_doc"))
    if calibration_count != 20:
        errors.append(f"Expected 20 calibration docs, got {calibration_count}")

    if errors:
        for e in errors[:10]:
            logger.error(e)
        raise ValueError(f"{len(errors)} validation errors")
    logger.info("Validation PASSED")


def build_mini(rows: list[dict]) -> list[dict]:
    folio_val = [r for r in rows if r["metadata_fold"] == "folio" and r["split"] in ("val", "calibration")]
    folio_cal = [r for r in folio_val if r["is_calibration_doc"]]
    folio_non_cal = [r for r in folio_val if not r["is_calibration_doc"]]

    labels = ["True", "False", "Unknown"]
    selected = []
    # ~17 per label from FOLIO val + calibration, balanced
    for label in labels:
        group = [r for r in folio_non_cal if r["gold_label"] == label][:17]
        selected.extend(group)
    selected.extend(folio_cal[:20])  # include calibration docs

    # Trim to 50
    mini = selected[:50]
    logger.info(f"Mini dataset: {len(mini)} rows")
    return mini


def build_preview(rows: list[dict]) -> list[dict]:
    folio = [r for r in rows if r["metadata_fold"] == "folio"][:5]
    proofwriter = [r for r in rows if r["metadata_fold"] == "proofwriter"][:5]
    preview = folio + proofwriter
    logger.info(f"Preview dataset: {len(preview)} rows")
    return preview


def log_stats(rows: list[dict]) -> None:
    folio_rows = [r for r in rows if r["metadata_fold"] == "folio"]
    pw_rows = [r for r in rows if r["metadata_fold"] == "proofwriter"]
    folio_labels = collections.Counter(r["gold_label"] for r in folio_rows)
    splits = collections.Counter(r["split"] for r in rows)
    logger.info(f"Total rows: {len(rows)}")
    logger.info(f"FOLIO rows: {len(folio_rows)}, labels: {dict(folio_labels)}")
    logger.info(f"ProofWriter rows: {len(pw_rows)}")
    logger.info(f"Splits: {dict(splits)}")
    cal = sum(1 for r in rows if r["is_calibration_doc"])
    logger.info(f"Calibration docs: {cal}")


@logger.catch(reraise=True)
def main() -> None:
    logger.info("Building FOLIO + ProofWriter-NL unified dataset")

    logger.info("Loading FOLIO-KR...")
    folio_rows = load_folio()

    logger.info("Assigning calibration subset...")
    folio_rows = assign_calibration(folio_rows)

    logger.info("Loading RuleTaker depth-3 NatLang...")
    pw_rows = load_ruletaker_depth3(max_examples=300)

    all_rows = folio_rows + pw_rows
    log_stats(all_rows)

    logger.info("Validating...")
    validate(all_rows)

    # Save full
    out = WORKSPACE / "data_out.json"
    out.write_text(json.dumps(all_rows, indent=2, ensure_ascii=False))
    logger.info(f"Saved data_out.json: {len(all_rows)} rows ({out.stat().st_size / 1e6:.1f} MB)")

    # Save mini
    mini = build_mini(all_rows)
    (WORKSPACE / "data_out_mini.json").write_text(json.dumps(mini, indent=2, ensure_ascii=False))
    logger.info(f"Saved data_out_mini.json: {len(mini)} rows")

    # Save preview
    preview = build_preview(all_rows)
    (WORKSPACE / "data_out_preview.json").write_text(json.dumps(preview, indent=2, ensure_ascii=False))
    logger.info(f"Saved data_out_preview.json: {len(preview)} rows")

    # Save metadata sidecar
    metadata = {
        "datasets": [
            {
                "name": "FOLIO",
                "source": "HannaAbiAkl/FOLIO-KR",
                "original_source": "yale-nlp/FOLIO (gated)",
                "note": "HannaAbiAkl/FOLIO-KR mirrors yale-nlp/FOLIO with additional KR notations",
                "paper": "https://arxiv.org/abs/2209.00840",
                "rows": len(folio_rows),
                "metadata_fold": "folio",
            },
            {
                "name": "ProofWriter-NL (RuleTaker depth-3ext-NatLang)",
                "source": "tasksource/ruletaker",
                "original_source": "allenai/ruletaker",
                "paper": "https://arxiv.org/abs/2012.13048",
                "rows": len(pw_rows),
                "metadata_fold": "proofwriter",
                "config": "depth-3ext-NatLang",
            },
        ],
        "total_rows": len(all_rows),
        "calibration_count": sum(1 for r in all_rows if r["is_calibration_doc"]),
    }
    (WORKSPACE / "metadata.json").write_text(json.dumps(metadata, indent=2))
    logger.info("Saved metadata.json")
    logger.info("Done.")


if __name__ == "__main__":
    main()
