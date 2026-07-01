#!/usr/bin/env python3
"""Stage 3 QC: audit patient-level wide radiomics feature tables.

This tool is intentionally table-first for clinical/hospital use. It checks
whether the expected number of patient rows and modality-prefixed feature
columns were produced, and writes CSVs that expose all final wide features for
review.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    import pandas as pd
    import yaml
except ImportError as error:  # pragma: no cover
    print(f"Missing required package: {error}", file=sys.stderr)
    raise


SAMPLE_METADATA_COLUMNS = {
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
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 3 QC: check wide-table row counts and feature counts."
    )
    parser.add_argument(
        "features_dir",
        nargs="?",
        type=Path,
        default=Path("radiomics_features"),
        help="Stage 2 output directory. Default: radiomics_features",
    )
    parser.add_argument(
        "manifest_dir",
        nargs="?",
        type=Path,
        default=Path("radiomics_manifests"),
        help="Stage 1 manifest directory. Default: radiomics_manifests",
    )
    parser.add_argument(
        "config_path",
        nargs="?",
        type=Path,
        default=Path("radiomics_config.yaml"),
        help="Pipeline config. Default: radiomics_config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="QC output directory. Default: <features_dir>/qc_features",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"Config must contain a YAML mapping: {path}")
    return config


def configured_modalities(config: dict[str, Any]) -> list[str]:
    modalities = config.get("modalities", {})
    if not isinstance(modalities, dict) or not modalities:
        raise ValueError("Config key modalities must be a non-empty mapping.")
    return [str(name) for name in modalities]


def augmentation_samples_per_patient(config: dict[str, Any]) -> int:
    augmentation = config.get("augmentation", {}) or {}
    if not isinstance(augmentation, dict) or not bool(augmentation.get("enabled", False)):
        return 0
    return int(augmentation.get("samples_per_patient", 0))


def load_manifest_summary(manifest_dir: Path) -> dict[str, Any]:
    summary_path = manifest_dir / "summary.json"
    if not summary_path.exists():
        manifest_paths = sorted(
            path for path in manifest_dir.glob("*.json") if path.name != "summary.json"
        )
        return {
            "patient_count": len(manifest_paths),
            "patients": [
                {"patient_id": path.stem, "status": "unknown"} for path in manifest_paths
            ],
        }
    with summary_path.open("r", encoding="utf-8") as stream:
        summary = json.load(stream)
    if not isinstance(summary, dict):
        raise ValueError(f"Manifest summary must contain a JSON object: {summary_path}")
    return summary


def read_csv_if_exists(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path)


def feature_columns(df: pd.DataFrame | None) -> list[str]:
    if df is None:
        return []
    return [column for column in df.columns if column not in SAMPLE_METADATA_COLUMNS]


def status(ok: bool) -> str:
    return "OK" if ok else "CHECK"


def patient_id_column(df: pd.DataFrame) -> str:
    for column in ("source_subject_id", "subject_id", "sample_id"):
        if column in df.columns:
            return column
    raise ValueError("Wide CSV must contain source_subject_id, subject_id, or sample_id.")


def feature_counts_by_modality(features: list[str], modalities: list[str]) -> dict[str, int]:
    counts = {}
    for modality in modalities:
        prefix = f"{modality}_"
        counts[modality] = sum(1 for feature in features if feature.startswith(prefix))
    return counts


def write_key_value_csv(path: Path, values: list[tuple[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["metric", "value"])
        writer.writerows(values)


def write_patient_row_counts(
    path: Path,
    wide_df: pd.DataFrame,
    patients: list[dict[str, Any]],
) -> None:
    patient_ids = [str(patient.get("patient_id", "")) for patient in patients]
    observed = wide_df.groupby(patient_id_column(wide_df)).size()
    rows = []
    for patient_id in sorted(patient_ids, key=lambda value: value.casefold()):
        count = int(observed.get(patient_id, 0))
        rows.append(
            {
                "patient_id": patient_id,
                "observed_rows": count,
                "expected_rows": 1,
                "status": status(count == 1),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def write_modality_feature_counts(
    path: Path,
    features: list[str],
    modalities: list[str],
) -> dict[str, int]:
    counts = feature_counts_by_modality(features, modalities)
    expected = max(counts.values()) if counts else 0
    rows = [
        {
            "modality": modality,
            "feature_columns": counts[modality],
            "expected_feature_columns": expected,
            "status": status(counts[modality] == expected and expected > 0),
        }
        for modality in modalities
    ]
    pd.DataFrame(rows).to_csv(path, index=False)
    return counts


def write_all_feature_columns(path: Path, wide_df: pd.DataFrame, features: list[str]) -> None:
    numeric = (
        wide_df[features].apply(pd.to_numeric, errors="coerce")
        if features
        else pd.DataFrame()
    )
    rows: list[dict[str, Any]] = []
    for feature in features:
        values = numeric[feature]
        rows.append(
            {
                "feature_name": feature,
                "non_empty_rows": int(wide_df[feature].notna().sum()),
                "numeric_rows": int(values.notna().sum()),
                "missing_rows": int(values.isna().sum()),
                "mean": values.mean(skipna=True),
                "std": values.std(skipna=True),
                "min": values.min(skipna=True),
                "max": values.max(skipna=True),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def main() -> int:
    args = parse_args()
    features_dir = args.features_dir.resolve()
    manifest_dir = args.manifest_dir.resolve()
    config_path = args.config_path.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else features_dir / "qc_features"
    )

    wide_csv = features_dir / "radiomics_features_wide.csv"
    augmented_wide_csv = features_dir / "radiomics_features_wide_augmented.csv"
    errors_csv = features_dir / "radiomics_features_errors.csv"
    legacy_errors_csv = features_dir / "radiomics_features_long_errors.csv"

    if not wide_csv.exists():
        print(f"Wide feature CSV not found: {wide_csv}", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
        modalities = configured_modalities(config)
        samples_per_patient = augmentation_samples_per_patient(config)
        summary = load_manifest_summary(manifest_dir)
        patients = list(summary.get("patients", []))
        patient_count = int(summary.get("patient_count", len(patients)))

        wide_df = pd.read_csv(wide_csv)
        augmented_wide_df = read_csv_if_exists(augmented_wide_csv)
        errors_df = read_csv_if_exists(errors_csv)
        if errors_df is None:
            errors_df = read_csv_if_exists(legacy_errors_csv)

        if wide_df.empty:
            raise ValueError(f"Wide feature CSV is empty: {wide_csv}")

        wide_features = feature_columns(wide_df)
        modality_counts = feature_counts_by_modality(wide_features, modalities)
        per_modality_counts = list(modality_counts.values())
        per_modality_feature_count = max(per_modality_counts) if per_modality_counts else 0
        equal_modality_feature_counts = (
            bool(per_modality_counts)
            and all(count == per_modality_feature_count for count in per_modality_counts)
            and per_modality_feature_count > 0
        )

        expected_wide_rows = patient_count
        observed_wide_rows = len(wide_df)
        expected_wide_features = per_modality_feature_count * len(modalities)
        observed_wide_features = len(wide_features)
        expected_augmented_rows = patient_count * samples_per_patient if samples_per_patient > 0 else 0
        observed_augmented_rows = 0 if augmented_wide_df is None else len(augmented_wide_df)
        augmented_features = feature_columns(augmented_wide_df)
        error_rows = 0 if errors_df is None else len(errors_df)

        output_dir.mkdir(parents=True, exist_ok=True)
        for stale_name in [
            "03_modality_row_counts.csv",
            "05_all_feature_values_long.csv",
            "06_all_feature_values_wide.csv",
        ]:
            stale_path = output_dir / stale_name
            if stale_path.exists():
                stale_path.unlink()

        write_key_value_csv(
            output_dir / "01_qc_summary.csv",
            [
                ("features_dir", features_dir),
                ("manifest_dir", manifest_dir),
                ("config_path", config_path),
                ("configured_modalities", ",".join(modalities)),
                ("patient_count_from_manifests", patient_count),
                ("expected_wide_rows", expected_wide_rows),
                ("observed_wide_rows", observed_wide_rows),
                ("wide_row_count_status", status(observed_wide_rows == expected_wide_rows)),
                ("feature_columns_per_modality", per_modality_feature_count),
                ("expected_wide_feature_columns", expected_wide_features),
                ("observed_wide_feature_columns", observed_wide_features),
                (
                    "wide_feature_count_status",
                    status(
                        observed_wide_features == expected_wide_features
                        and equal_modality_feature_counts
                    ),
                ),
                (
                    "expected_augmented_wide_rows",
                    expected_augmented_rows,
                ),
                ("observed_augmented_wide_rows", observed_augmented_rows),
                (
                    "augmented_wide_row_count_status",
                    status(observed_augmented_rows == expected_augmented_rows),
                ),
                ("augmented_wide_feature_columns", len(augmented_features)),
                ("stage2_error_rows", error_rows),
            ],
        )
        write_patient_row_counts(output_dir / "02_patient_row_counts.csv", wide_df, patients)
        write_modality_feature_counts(
            output_dir / "03_modality_feature_counts.csv",
            wide_features,
            modalities,
        )
        write_all_feature_columns(
            output_dir / "04_all_feature_columns.csv",
            wide_df,
            wide_features,
        )

        shutil.copy2(wide_csv, output_dir / "05_all_feature_values_wide.csv")
        if augmented_wide_csv.exists():
            shutil.copy2(
                augmented_wide_csv,
                output_dir / "06_all_feature_values_wide_augmented.csv",
            )
        else:
            stale_augmented_copy = output_dir / "06_all_feature_values_wide_augmented.csv"
            if stale_augmented_copy.exists():
                stale_augmented_copy.unlink()
        if errors_csv.exists():
            shutil.copy2(errors_csv, output_dir / "07_stage2_errors.csv")
        elif legacy_errors_csv.exists():
            shutil.copy2(legacy_errors_csv, output_dir / "07_stage2_errors.csv")
        else:
            stale_errors_copy = output_dir / "07_stage2_errors.csv"
            if stale_errors_copy.exists():
                stale_errors_copy.unlink()

        print(f"Stage 3 QC complete: {output_dir}")
        print(f"  Wide rows: {observed_wide_rows}/{expected_wide_rows}")
        print(f"  Feature columns per modality: {per_modality_feature_count}")
        print(f"  Wide feature columns: {observed_wide_features}/{expected_wide_features}")
        if samples_per_patient > 0:
            print(f"  Augmented wide rows: {observed_augmented_rows}/{expected_augmented_rows}")
        print(f"  Stage 2 error rows: {error_rows}")
        return 0
    except Exception as exc:
        print(f"stage3_qc_features.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
