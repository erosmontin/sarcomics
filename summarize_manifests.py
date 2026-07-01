#!/usr/bin/env python3
"""Summarize per-patient radiomics input manifests.

The output is a batch-level JSON file with one row per patient and modality
counts that are useful before the real radiomics extraction step is added.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a JSON summary from patient recognition manifests."
    )
    parser.add_argument("manifest_dir", type=Path, help="Directory containing patient JSON files.")
    parser.add_argument("output_json", type=Path, help="Where to write the summary JSON.")
    parser.add_argument(
        "--print-table",
        action="store_true",
        help="Also print a compact per-patient count table to stdout.",
    )
    return parser.parse_args()


def selected_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    return 1


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must contain a JSON object: {path}")
    return data


def entry_counts(entry: dict[str, Any]) -> dict[str, int | bool]:
    candidates = entry.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    return {
        "selected_count": selected_count(entry.get("selected")),
        "candidate_count": len(candidates),
        "missing": bool(entry.get("missing", False)),
    }


def build_summary(manifest_dir: Path) -> dict[str, Any]:
    manifest_paths = [
        path
        for path in sorted(manifest_dir.glob("*.json"), key=lambda item: item.name.casefold())
        if path.name != "summary.json"
    ]

    patients: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    modality_totals: dict[str, Counter[str]] = {}
    roi_totals: Counter[str] = Counter()

    for path in manifest_paths:
        manifest = load_manifest(path)
        patient_id = str(manifest.get("patient_id", path.stem))
        status = str(manifest.get("status", "unknown"))
        status_counts[status] += 1

        roi = entry_counts(manifest.get("roi", {}))
        roi_totals["selected_images"] += int(roi["selected_count"])
        roi_totals["candidates"] += int(roi["candidate_count"])
        roi_totals["missing"] += int(bool(roi["missing"]))

        modality_counts: dict[str, dict[str, int | bool]] = {}
        total_selected_images = 0
        images = manifest.get("images", {})
        if not isinstance(images, dict):
            images = {}

        for modality, entry in images.items():
            counts = entry_counts(entry if isinstance(entry, dict) else {})
            modality_counts[str(modality)] = counts
            total_selected_images += int(counts["selected_count"])

            totals = modality_totals.setdefault(str(modality), Counter())
            totals["selected_images"] += int(counts["selected_count"])
            totals["candidates"] += int(counts["candidate_count"])
            totals["missing"] += int(bool(counts["missing"]))
            if int(counts["selected_count"]) > 0:
                totals["patients_with_selected"] += 1

        patients.append(
            {
                "patient_id": patient_id,
                "status": status,
                "manifest": str(path),
                "roi": roi,
                "modalities": modality_counts,
                "total_selected_images": total_selected_images,
                "missing_required": manifest.get("missing_required", []),
                "missing_optional": manifest.get("missing_optional", []),
            }
        )

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_dir": str(manifest_dir),
        "patient_count": len(patients),
        "status_counts": dict(sorted(status_counts.items())),
        "roi_totals": dict(roi_totals),
        "modality_totals": {
            modality: dict(totals)
            for modality, totals in sorted(modality_totals.items())
        },
        "patients": patients,
    }


def print_table(summary: dict[str, Any]) -> None:
    modality_names = ordered_modalities(list(summary.get("modality_totals", {}).keys()))
    header = ["patient_id", "status", "roi", *modality_names, "total"]
    print("\t".join(header))
    for patient in summary.get("patients", []):
        modalities = patient.get("modalities", {})
        row = [
            str(patient.get("patient_id", "")),
            str(patient.get("status", "")),
            str(patient.get("roi", {}).get("selected_count", 0)),
        ]
        for modality in modality_names:
            row.append(str(modalities.get(modality, {}).get("selected_count", 0)))
        row.append(str(patient.get("total_selected_images", 0)))
        print("\t".join(row))


def ordered_modalities(modality_names: list[str]) -> list[str]:
    preferred = ["t1w", "t1wc", "t2w", "adc"]
    known = [name for name in preferred if name in modality_names]
    extra = sorted(
        (name for name in modality_names if name not in preferred),
        key=lambda item: item.casefold(),
    )
    return known + extra


def main() -> int:
    args = parse_args()
    manifest_dir = args.manifest_dir.resolve()
    output_json = args.output_json.resolve()

    if not manifest_dir.is_dir():
        print(f"Manifest directory not found: {manifest_dir}", file=sys.stderr)
        return 2

    try:
        summary = build_summary(manifest_dir)
    except Exception as exc:
        print(f"summarize_manifests.py: {exc}", file=sys.stderr)
        return 1

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")

    if args.print_table:
        print_table(summary)

    print(f"Summary manifest: {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
