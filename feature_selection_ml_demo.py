#!/usr/bin/env python3
"""
Technical smoke test for radiomics feature selection and multiclass ML.

This script:
1. Reads the original and optional augmented long-format radiomics CSV files.
2. Pivots modality rows into one wide row per sample.
3. Creates a temporary fake target y with classes 0, 1, and 2.
4. Cleans numeric features using pandas.
5. Selects the top features using ANOVA F-scores on the training set only.
6. Trains several simple multiclass classifiers.
7. Saves datasets, selected features, predictions, metrics, and fitted models.

IMPORTANT
---------
The fake labels are determined from source_subject_id and have no clinical
meaning. With only a few source patients, the resulting accuracy is not a
scientific performance estimate. This is intended only to verify that the
end-to-end analysis code runs successfully.

Example
-------
python feature_selection_ml_demo.py \
    --original radiomics_features_long.csv \
    --augmented radiomics_features_long_augmented.csv \
    --output-dir ml_demo_output \
    --top-k 50
"""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_selection import f_classif
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC


# Columns that describe the extraction/sample rather than radiomic measurements.
METADATA_COLUMNS = {
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
    "image_name",
    "image_path",
    "resampled_image_path",
    "roi_path",
    "resampled_roi_path",
    "manifest_path",
    "bin_counts",
    "extractor",
    "feature_status",
    "feature_warnings",
    "normalized",
    "normalize_scale",
    "pyfe_radius",
    "glcm_distances",
    "image_types",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a simple feature-selection and multiclass ML smoke test."
    )
    parser.add_argument(
        "--original",
        type=Path,
        default=Path("radiomics_features_long.csv"),
        help="Original long-format radiomics CSV.",
    )
    parser.add_argument(
        "--augmented",
        type=Path,
        default=Path("radiomics_features_long_augmented.csv"),
        help=(
            "Optional augmented long-format CSV. It is used when present. "
            "Pass a nonexistent path to run without it."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("ml_demo_output"),
        help="Directory for generated outputs.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=50,
        help="Maximum number of features selected using training data.",
    )
    parser.add_argument(
        "--test-size",
        type=float,
        default=1.0 / 3.0,
        help="Fraction of samples assigned to the technical test set.",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=42,
        help="Random seed.",
    )
    return parser.parse_args()


def read_long_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"CSV not found: {path}")

    print(f"Reading: {path}")
    frame = pd.read_csv(path, low_memory=False)

    required = {"sample_id", "source_subject_id", "image_name"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")

    if "feature_status" in frame.columns:
        failed = frame["feature_status"].fillna("").ne("ok")
        if failed.any():
            print(
                f"Warning: excluding {int(failed.sum())} rows whose "
                "feature_status is not 'ok'."
            )
            frame = frame.loc[~failed].copy()

    return frame


def find_feature_columns(frame: pd.DataFrame) -> list[str]:
    """
    Keep only numeric radiomic columns and exclude extraction/augmentation metadata.
    """
    numeric_columns = frame.select_dtypes(include=[np.number]).columns
    feature_columns = [
        column for column in numeric_columns if column not in METADATA_COLUMNS
    ]

    if not feature_columns:
        raise ValueError("No numeric radiomic feature columns were detected.")

    return feature_columns


def long_to_wide(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Convert:
        one row per sample/modality
    into:
        one row per sample with modality-prefixed feature columns.
    """
    feature_columns = find_feature_columns(frame)

    identifiers = frame[
        ["sample_id", "source_subject_id", "image_name"]
    ].copy()

    if identifiers.duplicated(["sample_id", "image_name"]).any():
        duplicates = identifiers.loc[
            identifiers.duplicated(["sample_id", "image_name"], keep=False)
        ]
        raise ValueError(
            "More than one row exists for a sample/modality combination:\n"
            f"{duplicates.head(20).to_string(index=False)}"
        )

    values = frame[
        ["sample_id", "source_subject_id", "image_name", *feature_columns]
    ].copy()

    wide = values.set_index(
        ["sample_id", "source_subject_id", "image_name"]
    )[feature_columns].unstack("image_name")

    # Original MultiIndex: (feature_name, image_name)
    # New names: adc__feature_name, t1w__feature_name, ...
    wide.columns = [
        f"{image_name}__{feature_name}"
        for feature_name, image_name in wide.columns
    ]

    wide = wide.reset_index()
    return wide


def create_fake_target(
    frame: pd.DataFrame,
    number_of_classes: int = 3,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Assign a stable fake class from source_subject_id.

    Sorted source subjects receive 0, 1, 2, 0, 1, 2, ...
    Augmented versions inherit their source subject's class.
    """
    source_subjects = sorted(
        frame["source_subject_id"].dropna().astype(str).unique()
    )

    if len(source_subjects) < number_of_classes:
        raise ValueError(
            f"At least {number_of_classes} source subjects are required to "
            f"create fake classes 0..{number_of_classes - 1}; found "
            f"{len(source_subjects)}."
        )

    target_map = {
        subject: index % number_of_classes
        for index, subject in enumerate(source_subjects)
    }

    result = frame.copy()
    result["y"] = (
        result["source_subject_id"].astype(str).map(target_map).astype(int)
    )
    return result, target_map


def clean_feature_table(
    data: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str], dict[str, int]]:
    """
    Apply non-learned cleaning with pandas:
    - retain numeric feature columns,
    - replace infinity with NaN,
    - remove all-NaN columns,
    - remove columns with fewer than two distinct observed values.
    """
    excluded = {"sample_id", "source_subject_id", "y"}
    feature_columns = [
        column
        for column in data.columns
        if column not in excluded and pd.api.types.is_numeric_dtype(data[column])
    ]

    features = data[feature_columns].copy()
    features = features.replace([np.inf, -np.inf], np.nan)

    all_nan = features.columns[features.isna().all()].tolist()
    features = features.drop(columns=all_nan)

    observed_unique_counts = features.nunique(dropna=True)
    constant = observed_unique_counts[
        observed_unique_counts < 2
    ].index.tolist()
    features = features.drop(columns=constant)

    removal_summary = {
        "initial_numeric_features": len(feature_columns),
        "removed_all_nan": len(all_nan),
        "removed_constant": len(constant),
        "remaining_features": features.shape[1],
    }

    if features.empty:
        raise ValueError("No usable feature columns remain after cleaning.")

    return features, features.columns.tolist(), removal_summary


def split_data(
    features: pd.DataFrame,
    labels: pd.Series,
    metadata: pd.DataFrame,
    test_size: float,
    random_state: int,
):
    class_counts = labels.value_counts().sort_index()

    if (class_counts < 2).any():
        raise ValueError(
            "Each fake class needs at least two samples for a stratified split. "
            f"Class counts: {class_counts.to_dict()}"
        )

    indices = np.arange(len(features))

    train_indices, test_indices = train_test_split(
        indices,
        test_size=test_size,
        random_state=random_state,
        stratify=labels,
    )

    return (
        features.iloc[train_indices].copy(),
        features.iloc[test_indices].copy(),
        labels.iloc[train_indices].copy(),
        labels.iloc[test_indices].copy(),
        metadata.iloc[train_indices].copy(),
        metadata.iloc[test_indices].copy(),
    )


def select_features(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    x_test: pd.DataFrame,
    top_k: int,
):
    """
    Fit imputation and ANOVA selection on the training set only.
    """
    imputer = SimpleImputer(strategy="median")
    x_train_imputed = imputer.fit_transform(x_train)
    x_test_imputed = imputer.transform(x_test)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scores, p_values = f_classif(x_train_imputed, y_train)

    score_table = pd.DataFrame(
        {
            "feature": x_train.columns,
            "anova_f_score": scores,
            "p_value": p_values,
        }
    )

    # NaN/inf scores can occur for nearly constant features in tiny demo data.
    score_table["selection_score"] = (
        score_table["anova_f_score"]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(-np.inf)
    )

    available = int(np.isfinite(score_table["selection_score"]).sum())
    if available == 0:
        # Fall back to variance if the tiny demo dataset makes all ANOVA scores invalid.
        variances = pd.Series(
            np.nanvar(x_train_imputed, axis=0),
            index=x_train.columns,
        )
        score_table["selection_score"] = score_table["feature"].map(variances)
        score_table["selection_method"] = "training_variance_fallback"
    else:
        score_table["selection_method"] = "anova_f_classif"

    selected_count = min(top_k, len(score_table))
    selected_table = (
        score_table.sort_values(
            ["selection_score", "feature"],
            ascending=[False, True],
        )
        .head(selected_count)
        .reset_index(drop=True)
    )
    selected_table.insert(0, "rank", np.arange(1, len(selected_table) + 1))

    selected_features = selected_table["feature"].tolist()
    selected_positions = [
        x_train.columns.get_loc(feature) for feature in selected_features
    ]

    selected_train = x_train_imputed[:, selected_positions]
    selected_test = x_test_imputed[:, selected_positions]

    return (
        selected_train,
        selected_test,
        selected_table.drop(columns="selection_score"),
        imputer,
        selected_features,
    )


def build_models(random_state: int):
    return {
        "logistic_regression": LogisticRegression(
            max_iter=5000,
            class_weight="balanced",
            random_state=random_state,
        ),
        "linear_svm": SVC(
            kernel="linear",
            class_weight="balanced",
            probability=True,
            random_state=random_state,
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=300,
            class_weight="balanced",
            random_state=random_state,
            n_jobs=-1,
        ),
    }


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_dir = args.output_dir / "models"
    model_dir.mkdir(parents=True, exist_ok=True)

    original_long = read_long_csv(args.original)
    frames = [long_to_wide(original_long)]

    used_augmented = False
    if args.augmented.is_file():
        augmented_long = read_long_csv(args.augmented)
        frames.append(long_to_wide(augmented_long))
        used_augmented = True
    else:
        print(f"Augmented CSV not found; continuing without it: {args.augmented}")

    dataset = pd.concat(frames, ignore_index=True, sort=False)

    if dataset["sample_id"].duplicated().any():
        duplicate_ids = dataset.loc[
            dataset["sample_id"].duplicated(keep=False), "sample_id"
        ].tolist()
        raise ValueError(f"Duplicate sample_id values after concatenation: {duplicate_ids}")

    dataset, fake_target_map = create_fake_target(dataset)

    # Save the fully pivoted dataset with fake target.
    dataset.to_csv(
        args.output_dir / "dataset_wide_with_fake_y.csv",
        index=False,
    )

    pd.DataFrame(
        sorted(fake_target_map.items()),
        columns=["source_subject_id", "fake_y"],
    ).to_csv(args.output_dir / "fake_target_mapping.csv", index=False)

    features, feature_names, removal_summary = clean_feature_table(dataset)
    labels = dataset["y"]
    metadata = dataset[["sample_id", "source_subject_id", "y"]]

    (
        x_train,
        x_test,
        y_train,
        y_test,
        train_metadata,
        test_metadata,
    ) = split_data(
        features=features,
        labels=labels,
        metadata=metadata,
        test_size=args.test_size,
        random_state=args.random_state,
    )

    (
        x_train_selected,
        x_test_selected,
        selected_table,
        imputer,
        selected_features,
    ) = select_features(
        x_train=x_train,
        y_train=y_train,
        x_test=x_test,
        top_k=args.top_k,
    )

    selected_table.to_csv(
        args.output_dir / "selected_features.csv",
        index=False,
    )

    # Scaling is fitted after feature selection and on training data only.
    scaler = StandardScaler()
    x_train_scaled = scaler.fit_transform(x_train_selected)
    x_test_scaled = scaler.transform(x_test_selected)

    metrics_rows = []
    all_predictions = []

    for model_name, model in build_models(args.random_state).items():
        print(f"Training: {model_name}")

        if model_name == "random_forest":
            model.fit(x_train_selected, y_train)
            predictions = model.predict(x_test_selected)
            probabilities = model.predict_proba(x_test_selected)
        else:
            model.fit(x_train_scaled, y_train)
            predictions = model.predict(x_test_scaled)
            probabilities = model.predict_proba(x_test_scaled)

        metrics_rows.append(
            {
                "model": model_name,
                "accuracy": accuracy_score(y_test, predictions),
                "balanced_accuracy": balanced_accuracy_score(
                    y_test, predictions
                ),
                "macro_f1": f1_score(
                    y_test,
                    predictions,
                    average="macro",
                    zero_division=0,
                ),
                "n_train": len(y_train),
                "n_test": len(y_test),
                "n_selected_features": len(selected_features),
            }
        )

        model_predictions = test_metadata.reset_index(drop=True).copy()
        model_predictions.insert(0, "model", model_name)
        model_predictions["predicted_y"] = predictions

        for class_position, class_label in enumerate(model.classes_):
            model_predictions[f"probability_class_{class_label}"] = (
                probabilities[:, class_position]
            )

        all_predictions.append(model_predictions)

        report = classification_report(
            y_test,
            predictions,
            labels=[0, 1, 2],
            output_dict=True,
            zero_division=0,
        )
        pd.DataFrame(report).transpose().to_csv(
            args.output_dir / f"classification_report_{model_name}.csv"
        )

        matrix = confusion_matrix(y_test, predictions, labels=[0, 1, 2])
        pd.DataFrame(
            matrix,
            index=["true_0", "true_1", "true_2"],
            columns=["predicted_0", "predicted_1", "predicted_2"],
        ).to_csv(args.output_dir / f"confusion_matrix_{model_name}.csv")

        joblib.dump(
            model,
            model_dir / f"{model_name}.joblib",
        )

    metrics = pd.DataFrame(metrics_rows).sort_values(
        ["balanced_accuracy", "macro_f1", "model"],
        ascending=[False, False, True],
    )
    metrics.to_csv(args.output_dir / "model_metrics.csv", index=False)

    predictions_table = pd.concat(all_predictions, ignore_index=True)
    predictions_table.to_csv(
        args.output_dir / "test_predictions.csv",
        index=False,
    )

    # Save preprocessing objects and selected column names.
    joblib.dump(imputer, model_dir / "median_imputer.joblib")
    joblib.dump(scaler, model_dir / "standard_scaler.joblib")

    with (model_dir / "selected_feature_names.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(selected_features, stream, indent=2)

    summary = {
        "purpose": "technical smoke test only",
        "original_csv": str(args.original.resolve()),
        "augmented_csv": (
            str(args.augmented.resolve()) if used_augmented else None
        ),
        "samples_total": int(len(dataset)),
        "source_subjects": int(dataset["source_subject_id"].nunique()),
        "class_counts": {
            str(key): int(value)
            for key, value in labels.value_counts().sort_index().items()
        },
        "modalities_detected": sorted(
            original_long["image_name"].dropna().astype(str).unique().tolist()
        ),
        "used_augmented_data": used_augmented,
        "feature_cleaning": removal_summary,
        "selected_features": len(selected_features),
        "train_samples": int(len(y_train)),
        "test_samples": int(len(y_test)),
        "warning": (
            "Fake labels have no clinical meaning. Augmented samples from the "
            "same source subject can occur across the technical train/test split. "
            "Do not interpret these metrics as generalization performance."
        ),
    }

    with (args.output_dir / "run_summary.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(summary, stream, indent=2)

    print()
    print("Fake target mapping:")
    print(
        pd.DataFrame(
            sorted(fake_target_map.items()),
            columns=["source_subject_id", "fake_y"],
        ).to_string(index=False)
    )

    print()
    print("Model metrics:")
    print(metrics.to_string(index=False))

    print()
    print(f"Outputs saved to: {args.output_dir.resolve()}")
    print(
        "Reminder: these are technical smoke-test results, not valid "
        "scientific performance estimates."
    )


if __name__ == "__main__":
    main()
