#!/usr/bin/env python3
"""Optional QC helper: overlay ROI masks on images.

For each patient, extracts a representative image slice and the corresponding
ROI mask, overlays them with semi-transparency, and saves as PNG. Optionally
compares original vs augmented samples side-by-side.

Reads image and ROI paths from the long CSV produced by Stage 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    import SimpleITK as sitk
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
except ImportError as error:
    print(f"Missing required package: {error}", file=sys.stderr)
    raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate QC overlays: image + ROI on a representative slice."
    )
    parser.add_argument(
        "long_csv",
        type=Path,
        help="Long CSV from Stage 2 (radiomics_features_long.csv).",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="Directory to save QC overlays.",
    )
    parser.add_argument(
        "--augmented-csv",
        type=Path,
        default=None,
        help="Augmented long CSV for side-by-side comparison (radiomics_features_long_augmented.csv).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="DPI for saved figures. Default: 150",
    )
    parser.add_argument(
        "--alpha-roi",
        type=float,
        default=0.6,
        help="Alpha (transparency) for ROI overlay. Default: 0.6",
    )
    parser.add_argument(
        "--modality",
        default="t1w",
        help="Which modality to visualize. Default: t1w",
    )
    parser.add_argument(
        "--include-augmented",
        action="store_true",
        help="Also visualize augmented samples. Requires --augmented-csv.",
    )
    return parser.parse_args()


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in ("resampled_image_path", "roi_path", "subject_id", "sample_id", "image_name"):
        if col not in df.columns:
            raise ValueError(f"Expected column '{col}' not found in {path}")
    return df


def build_sample_index(df: pd.DataFrame, modality: str) -> dict[str, dict]:
    """Return {subject_id: {image_path, roi_path, sample_id}} for the chosen modality."""
    rows = df[df["image_name"].str.lower() == modality.lower()]
    index = {}
    for _, row in rows.iterrows():
        image_path = Path(str(row["resampled_image_path"]))
        roi_path = Path(str(row["roi_path"]))
        if image_path.exists() and roi_path.exists():
            index[str(row["subject_id"])] = {
                "image_path": image_path,
                "roi_path": roi_path,
                "sample_id": str(row["sample_id"]),
            }
    return index


def get_roi_slice(roi_array: np.ndarray) -> int:
    """Get the slice with the most ROI voxels along the z axis."""
    counts = np.count_nonzero(roi_array, axis=(1, 2))
    return int(np.argmax(counts)) if np.any(counts) else roi_array.shape[0] // 2


def normalize_for_display(arr: np.ndarray) -> np.ndarray:
    """Normalize 2D array to [0, 1] using 2nd–98th percentile."""
    vmin, vmax = np.percentile(arr, [2, 98])
    if vmax > vmin:
        return np.clip((arr - vmin) / (vmax - vmin), 0, 1)
    return np.zeros_like(arr, dtype=float)


def overlay_image_roi(
    ax: plt.Axes,
    image_path: Path,
    roi_path: Path,
    title: str,
    alpha_roi: float,
) -> bool:
    """Draw image + ROI overlay on ax. Returns False on error."""
    try:
        image_arr = sitk.GetArrayFromImage(sitk.ReadImage(str(image_path)))
        roi_arr = sitk.GetArrayFromImage(sitk.ReadImage(str(roi_path)))

        # If ROI has different shape, resample it onto image
        if image_arr.shape != roi_arr.shape:
            img_sitk = sitk.ReadImage(str(image_path))
            roi_sitk = sitk.ReadImage(str(roi_path))
            roi_sitk = sitk.Resample(
                roi_sitk, img_sitk, sitk.Transform(), sitk.sitkNearestNeighbor, 0.0, roi_sitk.GetPixelID()
            )
            roi_arr = sitk.GetArrayFromImage(roi_sitk)

        slc = get_roi_slice(roi_arr)
        ax.imshow(normalize_for_display(image_arr[slc]), cmap="gray")
        roi_masked = np.ma.masked_where(roi_arr[slc] == 0, roi_arr[slc].astype(float))
        ax.imshow(roi_masked, cmap="Reds", alpha=alpha_roi, vmin=0, vmax=1)
        ax.set_title(title, fontsize=9, fontweight="bold")
        ax.axis("off")
        return True
    except Exception as exc:
        ax.set_title(f"{title}\n(error)", fontsize=9, color="red")
        ax.axis("off")
        print(f"  Warning: {title}: {exc}")
        return False


def save_fig(fig: plt.Figure, path: Path, dpi: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    long_csv = args.long_csv.resolve()
    output_dir = args.output_dir.resolve()
    modality = args.modality.lower()

    if not long_csv.exists():
        print(f"Long CSV not found: {long_csv}", file=sys.stderr)
        return 2

    try:
        df_original = load_csv(long_csv)
    except Exception as exc:
        print(f"qc_roi_overlays.py: {exc}", file=sys.stderr)
        return 1

    original_index = build_sample_index(df_original, modality)
    if not original_index:
        print(f"No rows found for modality '{modality}' with existing files.", file=sys.stderr)
        return 2

    # Load augmented index if requested
    aug_by_subject: dict[str, list[dict]] = {}
    if args.include_augmented:
        aug_csv_path = args.augmented_csv
        if aug_csv_path is None:
            # Try default name next to the long CSV
            aug_csv_path = long_csv.with_name(long_csv.stem.replace("_long", "_long_augmented") + ".csv")
        if aug_csv_path.exists():
            df_aug = load_csv(aug_csv_path)
            # Only keep genuinely augmented rows
            if "is_augmented" in df_aug.columns:
                df_aug = df_aug[df_aug["is_augmented"].astype(str).str.lower() == "true"]
            aug_rows = df_aug[df_aug["image_name"].str.lower() == modality]
            for _, row in aug_rows.iterrows():
                subj = str(row["subject_id"])
                image_path = Path(str(row["resampled_image_path"]))
                roi_path = Path(str(row["roi_path"]))
                if image_path.exists() and roi_path.exists():
                    aug_by_subject.setdefault(subj, []).append({
                        "image_path": image_path,
                        "roi_path": roi_path,
                        "sample_id": str(row["sample_id"]),
                    })
        else:
            print(f"Warning: augmented CSV not found: {aug_csv_path}")

    total = len(original_index)
    success = 0

    for subject_id in sorted(original_index):
        orig = original_index[subject_id]
        aug_samples = aug_by_subject.get(subject_id, [])[:3]  # limit to 3 augmented

        n_panels = 1 + len(aug_samples)
        fig, axes = plt.subplots(1, n_panels, figsize=(6 * n_panels, 6))
        if n_panels == 1:
            axes = [axes]

        ok = overlay_image_roi(axes[0], orig["image_path"], orig["roi_path"],
                               f"{subject_id}\nOriginal ({modality})", args.alpha_roi)

        for idx, aug in enumerate(aug_samples, start=1):
            overlay_image_roi(axes[idx], aug["image_path"], aug["roi_path"],
                              f"{aug['sample_id']}\n({modality})", args.alpha_roi)

        if n_panels > 1:
            red_patch = mpatches.Patch(color="red", alpha=args.alpha_roi, label="ROI")
            fig.legend(handles=[red_patch], loc="lower right", fontsize=9)

        output_path = output_dir / f"{subject_id}_{modality}_overlay.png"
        save_fig(fig, output_path, args.dpi)
        if ok:
            success += 1
        print(f"  {subject_id}: {output_path.name}")

    print(f"\nQC visualization complete: {success}/{total} patients saved to {output_dir}")
    return 0 if success > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
