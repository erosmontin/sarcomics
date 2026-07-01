#!/usr/bin/env python3
"""Gather long radiomics rows into one wide CSV row per sample."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


SAMPLE_METADATA_COLUMNS = [
    "subject_id",
    "source_subject_id",
    "sample_id",
    "is_augmented",
    "augmentation_index",
    "rotation_x_deg",
    "rotation_y_deg",
    "rotation_z_deg",
    "translation_x_mm",
    "translation_y_mm",
    "translation_z_mm",
    "scale_x",
    "scale_y",
    "scale_z",
]

ROW_METADATA_COLUMNS = {
    "image_name",
    "image_path",
    "resampled_image_path",
    "roi_path",
    "resampled_roi_path",
    "manifest_path",
    "bin_counts",
    "extractor",
    "feature_status",
    "normalized",
    "normalize_scale",
    "pyfe_radius",
    "glcm_distances",
    "image_types",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Concatenate long radiomics rows into one row per sample."
    )
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("radiomics_config.yaml"),
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Keep samples even if a configured image is missing.",
    )
    parser.add_argument(
        "--incomplete-csv",
        type=Path,
        default=None,
    )
    return parser.parse_args()


def load_modalities(config_path: Path) -> tuple[list[str], bool]:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: python3 -m pip install pyyaml")
    with config_path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    modalities = config.get("modalities", {}) if isinstance(config, dict) else {}
    if not isinstance(modalities, dict) or not modalities:
        raise ValueError("Config key modalities must be a non-empty mapping.")
    return [str(name) for name in modalities], bool(config.get("require_complete_patients", True))


def read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not rows:
        raise ValueError(f"No rows found in {path}")
    return rows, fieldnames


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = [name for name in SAMPLE_METADATA_COLUMNS if any(name in row for row in rows)]
    feature_names = sorted({key for row in rows for key in row if key not in metadata})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*metadata, *feature_names])
        writer.writeheader()
        writer.writerows(rows)


def write_incomplete(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["sample_id", "source_subject_id", "missing_images"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    input_csv = args.input_csv.resolve()
    output_csv = args.output_csv.resolve()
    incomplete_csv = (
        args.incomplete_csv.resolve()
        if args.incomplete_csv
        else output_csv.with_name(output_csv.stem + "_incomplete.csv")
    )

    try:
        modalities, require_complete = load_modalities(args.config.resolve())
        rows, fieldnames = read_rows(input_csv)
    except Exception as exc:
        print(f"concatenate_radiomics_features.py: {exc}", file=sys.stderr)
        return 1

    allow_incomplete = args.allow_incomplete or not require_complete
    feature_columns = [
        name
        for name in fieldnames
        if name not in SAMPLE_METADATA_COLUMNS and name not in ROW_METADATA_COLUMNS
    ]

    by_sample: dict[str, dict[str, dict[str, str]]] = {}
    sample_metadata: dict[str, dict[str, str]] = {}
    for row in rows:
        sample_id = str(row.get("sample_id") or row.get("subject_id") or "").strip()
        image_name = str(row.get("image_name", "")).strip()
        if not sample_id or not image_name:
            continue
        sample_metadata.setdefault(
            sample_id,
            {column: row.get(column, "") for column in SAMPLE_METADATA_COLUMNS if column in row},
        )
        sample_images = by_sample.setdefault(sample_id, {})
        if image_name in sample_images:
            print(f"Duplicate sample/image row: {sample_id} {image_name}", file=sys.stderr)
            return 1
        sample_images[image_name] = row

    wide_rows: list[dict[str, Any]] = []
    incomplete_rows: list[dict[str, str]] = []

    for sample_id in sorted(by_sample, key=lambda value: value.casefold()):
        sample_images = by_sample[sample_id]
        missing = [image for image in modalities if image not in sample_images]
        metadata = sample_metadata.get(sample_id, {"sample_id": sample_id})
        if missing:
            incomplete_rows.append(
                {
                    "sample_id": sample_id,
                    "source_subject_id": metadata.get("source_subject_id", sample_id),
                    "missing_images": ";".join(missing),
                }
            )
            if not allow_incomplete:
                continue

        output: dict[str, Any] = dict(metadata)
        for image_name in modalities:
            row = sample_images.get(image_name)
            if row is None:
                continue
            for feature in feature_columns:
                output[f"{image_name}_{feature}"] = row.get(feature, "")
        wide_rows.append(output)

    if not wide_rows:
        print("No sample rows were available for concatenation.", file=sys.stderr)
        return 1

    write_csv(output_csv, wide_rows)
    if incomplete_rows:
        write_incomplete(incomplete_csv, incomplete_rows)

    print(
        f"Concatenated {len(wide_rows)} samples -> {output_csv} "
        f"(incomplete samples: {len(incomplete_rows)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
