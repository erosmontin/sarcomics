#!/usr/bin/env python3
"""Extract radiomics features from manifest-recognized images.

Stage 1 is dataset description (`stage1_build_manifests.sh` + `runner.py`). This script is
Stage 2: it consumes those manifests, prepares feature images, lets PyFE handle
ROI normalization/resampling during extraction, and writes long CSV rows. It
does not segment, train models, or select features.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import logging
import math
import os
import sys
import traceback
import warnings
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-codex")
os.environ.setdefault("MPLBACKEND", "Agg")

try:
    import numpy as np
    import SimpleITK as sitk
    import yaml
    from tqdm import tqdm
except ImportError as error:  # pragma: no cover
    print(f"Missing required package: {error}", file=sys.stderr)
    raise


def configure_warning_filters() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"Fixed bin Count enabled!.*",
    )
    warnings.filterwarnings(
        "ignore",
        message=r"Applying `local_binary_pattern` to floating-point images.*",
        category=UserWarning,
        module=r"skimage\.feature\.texture",
    )
    warnings.filterwarnings(
        "ignore",
        message=r"Precision loss occurred in moment calculation.*",
        category=RuntimeWarning,
        module=r"radiomics\.imageoperations",
    )
    warnings.filterwarnings(
        "ignore",
        message=r"This figure includes Axes that are not compatible with tight_layout.*",
        category=UserWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"FigureCanvasAgg is non-interactive, and thus cannot be shown.*",
        category=UserWarning,
    )
    logging.getLogger("radiomics").setLevel(logging.ERROR)
    logging.getLogger("radiomics.featureextractor").setLevel(logging.ERROR)
    logging.getLogger("radiomics.imageoperations").setLevel(logging.ERROR)


def silence_pyradiomics() -> None:
    configure_warning_filters()
    try:
        import radiomics
    except ImportError:
        return
    radiomics.setVerbosity(logging.ERROR)
    logging.getLogger("radiomics").setLevel(logging.ERROR)


configure_warning_filters()

ALL_IMAGE_TYPES = [
    "Original",
    "LoG",
    "Wavelet",
    "Square",
    "SquareRoot",
    "Logarithm",
    "Exponential",
    "Gradient",
    "LBP2D",
    "LBP3D",
]

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

ROW_METADATA_COLUMNS = [
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
]

METADATA_COLUMNS = SAMPLE_METADATA_COLUMNS + ROW_METADATA_COLUMNS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract cached radiomics features from patient manifests."
    )
    parser.add_argument("manifest_dir", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("radiomics_config.yaml"),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Directory for resampled images, augmented samples, and cache.",
    )
    parser.add_argument(
        "--augmented-output-csv",
        type=Path,
        default=None,
        help="Optional CSV including original and augmented samples.",
    )
    parser.add_argument(
        "--errors-csv",
        type=Path,
        default=None,
        help="Optional CSV path for extraction errors.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of patient manifests to process in parallel. Default: 1.",
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


def import_pyfe_pyrad():
    try:
        import pyfe
        import pyfe.pyfe as pyfe_core
        from pyfe import PYRAD
    except ImportError as error:  # pragma: no cover - depends on local environment
        raise RuntimeError(
            "PyFE v3 is required for feature extraction. Install dependencies with: "
            "python3 -m pip install -r requirements.txt"
        ) from error

    # PyFE v3 references ``sitk`` inside PYRAD. Supplying it here keeps the
    # installed package usable without modifying the PyFE checkout.
    pyfe_core.sitk = sitk
    return pyfe, pyfe_core, PYRAD


def normalize_bin_counts(config: dict[str, Any]) -> list[int]:
    raw = config.get("bin_counts", config.get("bin_count", [32]))
    if isinstance(raw, int):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError("bin_counts must be a non-empty list or integer.")
    values: list[int] = []
    for value in raw:
        bin_count = int(value)
        if bin_count < 2:
            raise ValueError("Every bin count must be at least 2.")
        if bin_count not in values:
            values.append(bin_count)
    return values


def normalize_radii(config: dict[str, Any]) -> list[int]:
    raw = config.get(
        "glcm_distances",
        config.get("glcm_radii", config.get("pyfe_radius", 1)),
    )
    if isinstance(raw, int):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "glcm_distances/pyfe_radius must be a non-empty integer or list of integers."
        )
    values: list[int] = []
    for value in raw:
        radius = int(value)
        if radius < 1:
            raise ValueError("Every GLCM distance value must be at least 1.")
        if radius not in values:
            values.append(radius)
    return values


def normalize_image_types(config: dict[str, Any]) -> list[str]:
    raw = config.get("image_types", config.get("radiomics", {}).get("image_types", ["Original"]))
    if isinstance(raw, dict):
        selected = [str(name) for name in raw]
    elif isinstance(raw, str):
        selected = ALL_IMAGE_TYPES.copy() if raw.lower() == "all" else [raw]
    else:
        selected = [str(value) for value in raw]
        if len(selected) == 1 and selected[0].lower() == "all":
            selected = ALL_IMAGE_TYPES.copy()
    unknown = sorted(set(selected) - set(ALL_IMAGE_TYPES))
    if unknown:
        raise ValueError(f"Unknown image_types: {unknown}. Allowed: {ALL_IMAGE_TYPES} or 'all'.")
    return selected


def normalize_axis_ranges(raw_ranges: object, default_range: list[float], field: str) -> list[list[float]]:
    if raw_ranges is None:
        raw_ranges = [default_range] * 3
    if not isinstance(raw_ranges, list) or len(raw_ranges) != 3:
        raise ValueError(f"{field} must contain three [low, high] ranges.")
    output = []
    for axis_range in raw_ranges:
        if not isinstance(axis_range, list) or len(axis_range) != 2:
            raise ValueError(f"{field} must contain three [low, high] ranges.")
        low, high = [float(value) for value in axis_range]
        if high < low:
            raise ValueError(f"{field} range has high < low: {axis_range}")
        output.append([low, high])
    return output


def get_augmentation_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("augmentation", {}) or {}
    if not isinstance(raw, dict):
        raise ValueError("augmentation must be a mapping.")
    enabled = bool(raw.get("enabled", False))
    samples = int(raw.get("samples_per_patient", 0))
    if enabled and samples < 1:
        raise ValueError("augmentation.samples_per_patient must be at least 1 when enabled.")
    return {
        "enabled": enabled,
        "samples_per_patient": samples,
        "random_state": int(raw.get("random_state", 42)),
        "rotation_degrees": normalize_axis_ranges(raw.get("rotation_degrees"), [0.0, 0.0], "augmentation.rotation_degrees"),
        "translation_mm": normalize_axis_ranges(raw.get("translation_mm"), [0.0, 0.0], "augmentation.translation_mm"),
        "scale": normalize_axis_ranges(raw.get("scale"), [1.0, 1.0], "augmentation.scale"),
    }


def file_fingerprint(path: Path, hash_contents: bool) -> dict[str, Any]:
    stat = path.stat()
    payload: dict[str, Any] = {
        "path": str(path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    if hash_contents:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        payload["sha256"] = digest.hexdigest()
    return payload


def relevant_config(config: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "working_spacing_mm",
        "reference_image",
        "minimum_physical_overlap_fraction",
        "require_complete_patients",
        "bin_counts",
        "bin_count",
        "pyfe_radius",
        "glcm_distances",
        "glcm_radii",
        "normalize",
        "normalize_scale",
        "intensity_scaling",
        "image_types",
        "log_sigmas",
        "include_shape_features",
        "include_benford_features",
        "minimum_mask_voxels",
        "feature_cache",
        "augmentation",
        "radiomics",
    ]
    return {key: config.get(key) for key in keys if key in config}


def make_signature(
    sample_id: str,
    image_paths: dict[str, Path],
    roi_path: Path,
    config: dict[str, Any],
    sample_metadata: dict[str, Any],
) -> dict[str, Any]:
    cache_config = config.get("feature_cache", {}) or {}
    hash_contents = bool(cache_config.get("hash_file_contents", False))
    payload = {
        "cache_version": 5,
        "sample_id": sample_id,
        "source_images": {
            name: file_fingerprint(path, hash_contents)
            for name, path in sorted(image_paths.items())
            if path.exists()
        },
        "roi": file_fingerprint(roi_path, hash_contents),
        "config": relevant_config(config),
        "sample_metadata": {key: str(value) for key, value in sorted(sample_metadata.items())},
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return {
        "algorithm": "sha256",
        "hash": hashlib.sha256(encoded).hexdigest(),
        "payload": payload,
    }


def safe_name(value: object) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value))
    return cleaned.strip("._") or "unnamed"


def manifest_paths(manifest_dir: Path) -> list[Path]:
    return [
        path
        for path in sorted(manifest_dir.glob("*.json"), key=lambda item: item.name.casefold())
        if path.name != "summary.json"
    ]


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must contain a JSON object: {path}")
    return data


def selected_path(patient_dir: Path, entry: dict[str, Any]) -> Path | None:
    selected = entry.get("selected")
    if selected is None or isinstance(selected, list):
        return None
    path = Path(str(selected))
    if not path.is_absolute():
        path = patient_dir / path
    return path.resolve()


def image_physical_bounds(image: sitk.Image) -> tuple[np.ndarray, np.ndarray]:
    size = image.GetSize()
    corners = np.asarray(
        [
            image.TransformIndexToPhysicalPoint((x, y, z))
            for x in (0, max(size[0] - 1, 0))
            for y in (0, max(size[1] - 1, 0))
            for z in (0, max(size[2] - 1, 0))
        ],
        dtype=float,
    )
    return corners.min(axis=0), corners.max(axis=0)


def physical_overlap_fraction(source: sitk.Image, target: sitk.Image) -> float:
    source_min, source_max = image_physical_bounds(source)
    target_min, target_max = image_physical_bounds(target)
    intersection = np.maximum(0.0, np.minimum(source_max, target_max) - np.maximum(source_min, target_min))
    intersection_volume = float(np.prod(intersection))
    source_volume = float(np.prod(np.maximum(source_max - source_min, 0.0)))
    target_volume = float(np.prod(np.maximum(target_max - target_min, 0.0)))
    denominator = min(source_volume, target_volume)
    return 0.0 if denominator <= 0 else intersection_volume / denominator


def get_intensity_scale(config: dict[str, Any], image_name: str) -> float:
    scaling = config.get("intensity_scaling", {}) or {}
    if not isinstance(scaling, dict):
        raise ValueError("intensity_scaling must be a mapping when present.")
    rule = scaling.get(image_name, {}) or {}
    if not isinstance(rule, dict):
        raise ValueError(f"intensity_scaling.{image_name} must be a mapping.")
    divide_by = float(rule.get("divide_by", 1.0))
    if divide_by <= 0:
        raise ValueError(f"intensity_scaling.{image_name}.divide_by must be positive.")
    return divide_by


def apply_intensity_scaling(image: sitk.Image, divide_by: float) -> sitk.Image:
    if divide_by == 1.0:
        return image
    scaled = sitk.Cast(image, sitk.sitkFloat32) / float(divide_by)
    scaled.CopyInformation(image)
    return scaled


def import_pyable(pyable_path: str | None):
    if pyable_path:
        package_parent = str(Path(pyable_path).expanduser().resolve().parent)
        if package_parent not in sys.path:
            sys.path.insert(0, package_parent)
    try:
        from pyable import Roiable, SITKImaginable, plotOverlay
    except ImportError as error:
        raise RuntimeError(
            "pyable is required for configured resampling. Set pyable_path in "
            "radiomics_config.yaml or install pyable on PYTHONPATH."
        ) from error
    return SITKImaginable, Roiable, plotOverlay


def get_qc_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("qc", {}) or {}
    if not isinstance(raw, dict):
        raise ValueError("qc must be a mapping when present.")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "alpha": float(raw.get("alpha", 0.45)),
        "dpi": int(raw.get("dpi", 120)),
    }


def roi_slice_index(mask_image: sitk.Image) -> int | None:
    mask = sitk.GetArrayFromImage(mask_image)
    if mask.ndim != 3:
        return None
    counts = np.count_nonzero(mask, axis=(1, 2))
    if not np.any(counts):
        return int(mask.shape[0] // 2)
    return int(np.argmax(counts))


def write_roi_qc_overlays(
    image_paths: dict[str, Path],
    roi_path: Path,
    roi_label: int,
    output_dir: Path,
    config: dict[str, Any],
    SITKImaginable,
    Roiable,
    plotOverlay,
) -> dict[str, dict[str, str]]:
    qc_config = get_qc_config(config)
    if not qc_config["enabled"]:
        return {}

    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, str]] = {}
    for image_name, image_path in image_paths.items():
        output_path = output_dir / f"{safe_name(image_name)}_roi_overlay.png"
        viewer = None
        try:
            image = SITKImaginable(str(image_path))
            roi = Roiable(str(roi_path), roivalue=int(roi_label))
            roi.resampleOnTargetImage(
                image.getImage(),
                interpolator=sitk.sitkNearestNeighbor,
                default_value=0.0,
                useNearestNeighborExtrapolator=False,
            )
            slice_idx = roi_slice_index(roi.getImage())

            with contextlib.redirect_stdout(io.StringIO()):
                viewer = plotOverlay(
                    image,
                    roi,
                    alpha=float(qc_config["alpha"]),
                    title=f"{image_name} ROI QC",
                    slice_idx=slice_idx,
                )
                viewer.saveFigure(str(output_path), dpi=int(qc_config["dpi"]))
            results[image_name] = {
                "qc_overlay_path": str(output_path),
                "qc_overlay_slice": "" if slice_idx is None else str(slice_idx),
                "qc_overlay_error": "",
            }
        except Exception as exc:
            results[image_name] = {
                "qc_overlay_path": "",
                "qc_overlay_slice": "",
                "qc_overlay_error": str(exc),
            }
        finally:
            fig = getattr(viewer, "fig", None)
            if fig is not None:
                import matplotlib.pyplot as plt

                plt.close(fig)
    return results


def prepare_feature_images(
    image_paths: dict[str, Path],
    roi_path: Path,
    reference_name: str,
    output_dir: Path,
    config: dict[str, Any],
) -> tuple[dict[str, Path], Path, list[dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    SITKImaginable, Roiable, plotOverlay = import_pyable(config.get("pyable_path"))

    reference_source = sitk.ReadImage(str(image_paths[reference_name]))
    if reference_source.GetDimension() != 3:
        raise ValueError(f"Reference image must be 3D: {image_paths[reference_name]}")

    target = SITKImaginable(str(image_paths[reference_name]))
    spacing = config.get("working_spacing_mm")
    if spacing is not None:
        spacing = [float(value) for value in spacing]
        if len(spacing) != 3 or any(value <= 0 for value in spacing):
            raise ValueError("working_spacing_mm must contain three positive values or null.")
        target.changeImageSpacing(spacing, interpolator=sitk.sitkLinear)
    target_image = target.getImage()

    geometry_rows: list[dict[str, Any]] = []
    resampled_paths: dict[str, Path] = {}
    minimum_overlap = float(config.get("minimum_physical_overlap_fraction", 0.70))

    for image_name, source_path in image_paths.items():
        source = sitk.ReadImage(str(source_path))
        overlap = physical_overlap_fraction(source, target_image)
        geometry_rows.append(
            {
                "image_name": image_name,
                "source_path": str(source_path),
                "physical_overlap_fraction": overlap,
                "source_size": "x".join(map(str, source.GetSize())),
                "source_spacing": "x".join(f"{value:.5g}" for value in source.GetSpacing()),
                "target_size": "x".join(map(str, target_image.GetSize())),
                "target_spacing": "x".join(f"{value:.5g}" for value in target_image.GetSpacing()),
                "below_minimum_overlap": overlap < minimum_overlap,
            }
        )

        output_path = output_dir / f"{image_name}_target_grid.nii.gz"
        divide_by = get_intensity_scale(config, image_name)
        if image_name == reference_name:
            output_image = target_image
        else:
            moving = SITKImaginable(str(source_path))
            moving.resampleOnTargetImage(
                target_image,
                interpolator=sitk.sitkLinear,
                default_value=0.0,
                useNearestNeighborExtrapolator=False,
            )
            output_image = moving.getImage()
        output_image = apply_intensity_scaling(output_image, divide_by)
        sitk.WriteImage(output_image, str(output_path), True)
        geometry_rows[-1]["intensity_divide_by"] = divide_by
        resampled_paths[image_name] = output_path

    roi_label = int(config.get("radiomics", {}).get("label", 1))
    qc_results = write_roi_qc_overlays(
        image_paths=resampled_paths,
        roi_path=roi_path,
        roi_label=roi_label,
        output_dir=output_dir.parent / "qc",
        config=config,
        SITKImaginable=SITKImaginable,
        Roiable=Roiable,
        plotOverlay=plotOverlay,
    )
    for row in geometry_rows:
        row.update(
            qc_results.get(
                str(row["image_name"]),
                {"qc_overlay_path": "", "qc_overlay_slice": "", "qc_overlay_error": ""},
            )
        )
    geometry_rows.append(
        {
            "image_name": "roi",
            "source_path": str(roi_path),
            "physical_overlap_fraction": "",
            "source_size": "",
            "source_spacing": "",
            "target_size": "x".join(map(str, target_image.GetSize())),
            "target_spacing": "x".join(f"{value:.5g}" for value in target_image.GetSpacing()),
            "below_minimum_overlap": "",
            "roi_label": roi_label,
            "roi_mask_voxels": mask_voxel_count(roi_path, roi_label),
            "roi_resampling": "pyfe_pyrad_runtime",
            "qc_overlay_path": "",
            "qc_overlay_slice": "",
            "qc_overlay_error": "",
        }
    )
    return resampled_paths, roi_path, geometry_rows


def create_zero_like(reference_path: Path, output_path: Path) -> Path:
    reference = sitk.ReadImage(str(reference_path))
    zero = sitk.Image(reference.GetSize(), reference.GetPixelID())
    zero.CopyInformation(reference)
    sitk.WriteImage(zero, str(output_path), True)
    return output_path


def mask_voxel_count(mask_path: Path, label: int) -> int:
    mask = sitk.GetArrayViewFromImage(sitk.ReadImage(str(mask_path)))
    return int(np.count_nonzero(mask == int(label)))


def pyradiomics_settings(
    config: dict[str, Any],
    bin_count: int,
    radius: int,
    normalize: bool,
    label: int,
) -> dict[str, Any]:
    return {
        "nbins": int(bin_count),
        "binCount": int(bin_count),
        "kernelRadius": [int(radius)],
        "distances": [int(radius)],
        "normalize": bool(normalize),
        "normalizeScale": float(config.get("normalize_scale", 100.0)),
        "label": int(label),
        "correctMask": True,
        "geometryTolerance": 1e-5,
        "additionalInfo": False,
        "resampledPixelSpacing": None,
        "minimumROISize": int(config.get("minimum_mask_voxels", 50)),
        "interpolator": sitk.sitkNearestNeighbor,
    }


def image_type_kwargs(config: dict[str, Any], image_type: str) -> dict[str, Any]:
    if image_type == "LoG":
        return {
            "sigma": [
                float(value)
                for value in config.get("log_sigmas", [1.0, 2.0, 3.0])
            ]
        }
    return {}


def initialize_pyrad_extractor(pyfe_core, settings: dict[str, Any]):
    silence_pyradiomics()
    initializer = getattr(pyfe_core, "initialize_pyrad_extractor", None)
    if initializer is not None:
        return initializer(settings)

    try:
        from radiomics import featureextractor
    except ImportError as error:
        raise RuntimeError(
            "PyRadiomics is required by PyFE PYRAD. Install dependencies with: "
            "python3 -m pip install -r requirements.txt"
        ) from error

    config: dict[str, Any] = {}
    if "bin" in settings:
        config["nbins"] = int(settings["bin"])
        config["binCount"] = int(settings["bin"])
    if "radius" in settings:
        radius = int(settings["radius"])
        config["kernelRadius"] = [radius]
        config["distances"] = [radius]
    if "normalize" in settings:
        config["normalize"] = bool(settings["normalize"])

    extractor = featureextractor.RadiomicsFeatureExtractor(**config)
    extractor.enableAllFeatures()
    extractor.enableAllImageTypes()
    pyfe_core._global_pyrad_extractor = extractor
    pyfe_core._global_pyrad_settings = dict(settings)
    return extractor


def execute_configured_pyfe_pyrad(worker, pyfe_core) -> OrderedDict[str, Any]:
    from radiomics import featureextractor

    settings = getattr(pyfe_core, "_global_pyrad_settings", None)
    if not isinstance(settings, dict):
        raise RuntimeError("PyFE PYRAD was called before extractor settings were configured.")

    image = pyfe_core.ima.Imaginable(worker.getImage())
    roi = pyfe_core.ima.Roiable(worker.getROI(), roivalue=worker.getROIvalue())
    if not image.isImaginableInTheSameSpace(roi):
        roi.resampleOnTargetImage(
            image.getImage(),
            interpolator=sitk.sitkNearestNeighbor,
            default_value=0.0,
            useNearestNeighborExtrapolator=False,
        )

    features: OrderedDict[str, Any] = OrderedDict()
    warnings_for_row: list[str] = []
    base_settings = dict(settings["pyradiomics"])
    image_types = list(settings["image_types"])
    skip_failed = bool(settings.get("skip_failed_image_types", True))
    image_type_options = dict(settings.get("image_type_kwargs", {}))

    def run_image_types(selected_types: list[str]) -> dict[str, Any]:
        extractor = featureextractor.RadiomicsFeatureExtractor(**base_settings)
        extractor.enableAllFeatures()
        extractor.disableAllImageTypes()
        for selected_type in selected_types:
            extractor.enableImageTypeByName(
                selected_type,
                customArgs=dict(image_type_options.get(selected_type, {})),
            )
        return extractor.execute(image.getImage(), roi.getImage())

    try:
        raw = run_image_types(image_types)
    except Exception as exc:
        if not skip_failed:
            raise
        warnings_for_row.append(f"combined_image_types: {exc}")
        raw = {}
        for image_type in image_types:
            try:
                raw.update(run_image_types([image_type]))
            except Exception as type_exc:
                warnings_for_row.append(f"{image_type}: {type_exc}")
                continue

    for key, value in raw.items():
        if not str(key).startswith("diagnostics_"):
            features[str(key)] = value

    worker._radiomics_int_warnings = warnings_for_row
    return features


def configured_image_types(config: dict[str, Any]) -> list[str]:
    selected = normalize_image_types(config)
    skip_types = config.get("skip_image_types", [])
    if isinstance(skip_types, str):
        skip_types = [skip_types]
    skip_types = {str(value) for value in skip_types}
    unknown = sorted(skip_types - set(ALL_IMAGE_TYPES))
    if unknown:
        raise ValueError(f"Unknown skip_image_types: {unknown}. Allowed: {ALL_IMAGE_TYPES}.")
    configured = [image_type for image_type in selected if image_type not in skip_types]
    if not configured:
        raise ValueError("At least one image type must remain after skip_image_types.")
    return configured


def skipped_image_types(config: dict[str, Any]) -> list[str]:
    selected = set(normalize_image_types(config))
    configured = set(configured_image_types(config))
    return [image_type for image_type in normalize_image_types(config) if image_type in selected - configured]


def patch_pyfe_pyrad(pyfe_core, pyrad_class) -> None:
    if getattr(pyrad_class, "_radiomics_int_configured", False):
        return

    def configured_get_pyrad(self):
        return execute_configured_pyfe_pyrad(self, pyfe_core)

    pyrad_class.getPYRAD = configured_get_pyrad
    pyrad_class._radiomics_int_configured = True


def configure_pyfe_extractor(config: dict[str, Any], bin_count: int, radius: int):
    silence_pyradiomics()
    pyfe, pyfe_core, pyrad_class = import_pyfe_pyrad()
    normalize = bool(config.get("normalize", True))
    label = int(config.get("radiomics", {}).get("label", 1))

    # PyFE maps ``radius`` onto PyRadiomics' GLCM ``distances`` internally.
    extractor = initialize_pyrad_extractor(
        pyfe_core,
        {
            "bin": int(bin_count),
            "radius": int(radius),
            "normalize": normalize,
        }
    )
    if extractor is None:
        raise RuntimeError("PyFE could not initialize its PyRadiomics extractor.")

    extractor.settings.update(
        pyradiomics_settings(
            config=config,
            bin_count=bin_count,
            radius=radius,
            normalize=normalize,
            label=label,
        )
    )
    extractor.settings.pop("binWidth", None)

    extractor.disableAllImageTypes()
    for image_type in configured_image_types(config):
        extractor.enableImageTypeByName(image_type, customArgs=image_type_kwargs(config, image_type))
    extractor.enableAllFeatures()
    pyfe_core._global_pyrad_settings = {
        "pyradiomics": pyradiomics_settings(
            config=config,
            bin_count=bin_count,
            radius=radius,
            normalize=normalize,
            label=label,
        ),
        "image_types": configured_image_types(config),
        "image_type_kwargs": {
            image_type: image_type_kwargs(config, image_type)
            for image_type in configured_image_types(config)
        },
        "skip_failed_image_types": bool(config.get("skip_failed_image_types", True)),
    }
    pyfe_core._global_pyrad_extractor = extractor
    patch_pyfe_pyrad(pyfe_core, pyrad_class)
    return pyrad_class, getattr(pyfe, "__version__", "3")


def extract_with_pyfe_pyrad(
    pyrad_class,
    image_path: Path,
    mask_path: Path,
    bin_count: int,
    normalize: bool,
    radius: int,
    label: int,
) -> tuple[OrderedDict[str, Any], list[str]]:
    worker = pyrad_class()
    worker.setImage(str(image_path))
    worker.setROI(str(mask_path))
    worker.setROIvalue(int(label))
    worker.setOptions(
        {
            "bin": int(bin_count),
            "radius": int(radius),
            "normalize": bool(normalize),
        }
    )

    raw = worker.getPYRAD()
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError(
            f"PyFE returned no features for image={image_path}, mask={mask_path}."
        )

    features: OrderedDict[str, Any] = OrderedDict()
    finite_values = 0
    for key, value in raw.items():
        key = str(key)
        if key.startswith("diagnostics_"):
            continue
        features[key] = serialize_value(value)
        if isinstance(value, (int, float, np.integer, np.floating)) and np.isfinite(
            float(value)
        ):
            finite_values += 1

    if finite_values == 0:
        raise RuntimeError(
            f"PyFE returned no finite radiomics values for image={image_path}, "
            f"mask={mask_path}."
        )
    return features, list(getattr(worker, "_radiomics_int_warnings", []))


def is_shape_feature(feature_name: str) -> bool:
    return "_shape_" in feature_name or "_shape2D_" in feature_name


def serialize_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    return str(value)


def extract_benford_features(
    image_path: Path,
    mask_path: Path,
    label: int,
    minimum_mask_voxels: int,
) -> OrderedDict[str, Any]:
    import contextlib
    import io

    try:
        from pyfe import BenfordFE
    except ImportError as error:
        raise RuntimeError(
            "PyFE v3 Benford features are unavailable. "
            "Install dependencies with: python3 -m pip install -r requirements.txt"
        ) from error

    _pyfe, pyfe_core, _pyrad_class = import_pyfe_pyrad()
    roi_values = np.asarray(
        pyfe_core.geRoiValues(str(image_path), str(mask_path), int(label)),
        dtype=float,
    )
    roi_values = roi_values[np.isfinite(roi_values)]
    if len(roi_values) < int(minimum_mask_voxels):
        raise ValueError(
            f"Mask contains only {len(roi_values)} finite voxels; "
            f"minimum is {minimum_mask_voxels}."
        )

    worker = BenfordFE()
    worker.setImage(str(image_path))
    worker.setROI(str(mask_path))
    worker.setROIvalue(int(label))

    with contextlib.redirect_stdout(io.StringIO()):
        raw = worker.getFeatures()
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError(
            f"PyFE returned no Benford features for image={image_path}, "
            f"mask={mask_path}."
        )

    domain_values = raw.get("Benford")
    if domain_values is None and len(raw) == 1:
        domain_values = next(iter(raw.values()))
    if not isinstance(domain_values, dict) or not domain_values:
        raise RuntimeError(
            f"Unexpected Benford feature output for image={image_path}, "
            f"mask={mask_path}: {raw!r}"
        )

    features: OrderedDict[str, Any] = OrderedDict()
    finite_values = 0
    for key, value in domain_values.items():
        value = serialize_value(value)
        features[f"benford_{key}"] = value
        if isinstance(value, (int, float, np.integer, np.floating)) and np.isfinite(
            float(value)
        ):
            finite_values += 1

    if finite_values == 0:
        raise RuntimeError(
            "PyFE produced no finite Benford features. Check the mask, image "
            "geometry, and the patient failure log."
        )
    return features


def extract_feature_row(
    sample_id: str,
    source_subject_id: str,
    image_name: str,
    image_path: Path,
    resampled_image_path: Path,
    roi_path: Path,
    resampled_roi_path: Path,
    manifest_path: Path,
    config: dict[str, Any],
    sample_metadata: dict[str, Any],
    force_nan_features: bool = False,
    show_progress: bool = False,
) -> OrderedDict[str, Any]:
    bin_counts = normalize_bin_counts(config)
    radii = normalize_radii(config)
    label = int(config.get("radiomics", {}).get("label", 1))
    minimum_voxels = int(config.get("minimum_mask_voxels", 50))

    row: OrderedDict[str, Any] = OrderedDict()
    row["subject_id"] = sample_id
    row["source_subject_id"] = source_subject_id
    row["sample_id"] = sample_id
    row["is_augmented"] = bool(sample_metadata.get("is_augmented", False))
    row["augmentation_index"] = int(sample_metadata.get("augmentation_index", -1))
    for field in [
        "rotation_x_deg",
        "rotation_y_deg",
        "rotation_z_deg",
        "translation_x_mm",
        "translation_y_mm",
        "translation_z_mm",
    ]:
        row[field] = float(sample_metadata.get(field, 0.0))
    for field in ["scale_x", "scale_y", "scale_z"]:
        row[field] = float(sample_metadata.get(field, 1.0))

    row["image_name"] = image_name
    row["image_path"] = str(image_path)
    row["resampled_image_path"] = str(resampled_image_path)
    row["roi_path"] = str(roi_path)
    row["resampled_roi_path"] = str(resampled_roi_path)
    row["manifest_path"] = str(manifest_path)
    row["bin_counts"] = ",".join(map(str, bin_counts))
    row["extractor"] = "pyfe_v3"
    row["feature_status"] = "missing_modality" if force_nan_features else "ok"
    row["feature_warnings"] = "; ".join(
        f"skipped image type {image_type}" for image_type in skipped_image_types(config)
    )
    row["normalized"] = bool(config.get("normalize", True))
    row["normalize_scale"] = float(config.get("normalize_scale", 100.0))
    row["pyfe_radius"] = ",".join(map(str, radii))
    row["glcm_distances"] = ",".join(map(str, radii))
    row["image_types"] = ",".join(configured_image_types(config))

    if force_nan_features:
        return row

    shape_written = False
    multi_radius = len(radii) > 1
    include_benford = bool(config.get("include_benford_features", True))
    progress_total = len(radii) * len(bin_counts) + (1 if include_benford else 0)
    progress_desc = f"{sample_id} {image_name}"
    progress_bar = tqdm(
        total=progress_total,
        desc=progress_desc,
        unit="step",
        leave=False,
        dynamic_ncols=True,
        disable=not show_progress,
    )
    try:
        for radius in radii:
            for bin_count in bin_counts:
                progress_bar.set_postfix_str(
                    f"PYRAD bin={bin_count} radius={radius}",
                    refresh=True,
                )
                pyrad_class, _pyfe_version = configure_pyfe_extractor(config, bin_count, radius)
                result, extraction_warnings = extract_with_pyfe_pyrad(
                    pyrad_class=pyrad_class,
                    image_path=resampled_image_path,
                    mask_path=resampled_roi_path,
                    bin_count=bin_count,
                    normalize=bool(config.get("normalize", True)),
                    radius=radius,
                    label=label,
                )
                if extraction_warnings:
                    row["feature_warnings"] = "; ".join(
                        warning
                        for warning in [row["feature_warnings"], *extraction_warnings]
                        if warning
                    )
                for key, value in result.items():
                    if is_shape_feature(key):
                        if bool(config.get("include_shape_features", True)) and not shape_written:
                            row[key] = value
                        continue
                    prefix = f"r{radius}_bin{bin_count}" if multi_radius else f"bin{bin_count}"
                    row[f"{prefix}_{key}"] = value
                shape_written = True
                progress_bar.update(1)

        if include_benford:
            progress_bar.set_postfix_str("PyFE Benford", refresh=True)
            row.update(extract_benford_features(resampled_image_path, resampled_roi_path, label, minimum_voxels))
            progress_bar.update(1)
    finally:
        progress_bar.close()

    return row


def sample_augmentation_parameters(rng: np.random.Generator, augmentation: dict[str, Any], index: int) -> dict[str, Any]:
    rotation = [float(rng.uniform(low, high)) for low, high in augmentation["rotation_degrees"]]
    translation = [float(rng.uniform(low, high)) for low, high in augmentation["translation_mm"]]
    scale = [float(rng.uniform(low, high)) for low, high in augmentation["scale"]]
    return {
        "is_augmented": True,
        "augmentation_index": int(index),
        "rotation_x_deg": rotation[0],
        "rotation_y_deg": rotation[1],
        "rotation_z_deg": rotation[2],
        "translation_x_mm": translation[0],
        "translation_y_mm": translation[1],
        "translation_z_mm": translation[2],
        "scale_x": scale[0],
        "scale_y": scale[1],
        "scale_z": scale[2],
    }


def make_affine_transform(reference_image: sitk.Image, parameters: dict[str, Any]) -> sitk.AffineTransform:
    size = reference_image.GetSize()
    center = reference_image.TransformContinuousIndexToPhysicalPoint([(axis - 1) / 2 for axis in size])
    rx, ry, rz = [math.radians(float(parameters[f"rotation_{axis}_deg"])) for axis in ("x", "y", "z")]
    sx, sy, sz = [float(parameters[f"scale_{axis}"]) for axis in ("x", "y", "z")]

    rotation_x = np.asarray([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
    rotation_y = np.asarray([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
    rotation_z = np.asarray([[math.cos(rz), -math.sin(rz), 0], [math.sin(rz), math.cos(rz), 0], [0, 0, 1]])
    matrix = rotation_z @ rotation_y @ rotation_x @ np.diag([sx, sy, sz])

    transform = sitk.AffineTransform(3)
    transform.SetCenter(center)
    transform.SetMatrix(matrix.reshape(-1).tolist())
    transform.SetTranslation([float(parameters[f"translation_{axis}_mm"]) for axis in ("x", "y", "z")])
    return transform


def resample_with_transform(source_path: Path, output_path: Path, transform: sitk.Transform, interpolator: int) -> Path:
    source = sitk.ReadImage(str(source_path))
    resampled = sitk.Resample(source, source, transform, interpolator, 0.0, source.GetPixelID())
    sitk.WriteImage(resampled, str(output_path), True)
    return output_path


def create_augmented_sample(
    resampled_images: dict[str, Path],
    resampled_roi: Path,
    reference_name: str,
    output_dir: Path,
    sample_id: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Path], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_image = sitk.ReadImage(str(resampled_images[reference_name]))
    transform = make_affine_transform(reference_image, parameters)

    augmented_images = {
        name: resample_with_transform(path, output_dir / f"{sample_id}_{name}.nii.gz", transform, sitk.sitkLinear)
        for name, path in resampled_images.items()
    }
    augmented_roi = resample_with_transform(
        resampled_roi,
        output_dir / f"{sample_id}_roi.nii.gz",
        transform,
        sitk.sitkNearestNeighbor,
    )
    return augmented_images, augmented_roi


def load_cache(cache_csv: Path, cache_manifest: Path, signature: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not cache_csv.exists() or not cache_manifest.exists():
        return None
    try:
        manifest = json.loads(cache_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("hash") != signature["hash"]:
        return None
    with cache_csv.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(METADATA_COLUMNS)
    feature_names = sorted({key for row in rows for key in row if key not in fieldnames})
    fieldnames.extend(feature_names)
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            str(row.get("sample_id", "")),
            str(row.get("image_name", "")),
            str(row.get("augmentation_index", "")),
        ),
    )
    output_rows: list[dict[str, Any]] = []
    for row in sorted_rows:
        output_row = dict(row)
        if output_row.get("feature_status") == "missing_modality":
            for feature_name in feature_names:
                output_row.setdefault(feature_name, "nan")
        output_rows.append(output_row)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)


def write_generic_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extract_sample_with_cache(
    sample_id: str,
    source_subject_id: str,
    original_images: dict[str, Path],
    feature_images: dict[str, Path],
    roi_path: Path,
    feature_roi: Path,
    manifest_path: Path,
    config: dict[str, Any],
    sample_metadata: dict[str, Any],
    cache_dir: Path,
    missing_modalities: set[str] | None = None,
    show_progress: bool = False,
) -> list[dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    signature = make_signature(sample_id, original_images, roi_path, config, sample_metadata)
    cache_csv = cache_dir / f"{safe_name(sample_id)}_radiomics_long.csv"
    cache_manifest = cache_dir / f"{safe_name(sample_id)}_feature_cache.json"
    cache_config = config.get("feature_cache", {}) or {}
    if bool(cache_config.get("enabled", True)):
        cached = load_cache(cache_csv, cache_manifest, signature)
        if cached is not None:
            return cached

    rows: list[dict[str, Any]] = []
    missing_modalities = missing_modalities or set()
    for image_name, image_path in feature_images.items():
        rows.append(
            extract_feature_row(
                sample_id=sample_id,
                source_subject_id=source_subject_id,
                image_name=image_name,
                image_path=original_images.get(image_name, image_path),
                resampled_image_path=image_path,
                roi_path=roi_path,
                resampled_roi_path=feature_roi,
                manifest_path=manifest_path,
                config=config,
                sample_metadata=sample_metadata,
                force_nan_features=image_name in missing_modalities,
                show_progress=show_progress,
            )
        )

    if bool(cache_config.get("enabled", True)):
        write_rows(cache_csv, rows)
        cache_manifest.write_text(json.dumps(signature, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return rows


def write_errors(path: Path, errors: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["patient_id", "sample_id", "manifest_path", "error", "traceback"],
        )
        writer.writeheader()
        writer.writerows(errors)


def process_manifest(
    manifest_path: Path,
    config: dict[str, Any],
    work_root: Path,
    show_progress: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    manifest = load_manifest(manifest_path)
    patient_id = str(manifest.get("patient_id", manifest_path.stem))
    patient_dir_raw = manifest.get("patient_dir")
    if patient_dir_raw is None:
        raise ValueError(f"Manifest missing required field 'patient_dir': {manifest_path}")
    patient_dir = Path(str(patient_dir_raw))
    modalities = configured_modalities(config)
    require_complete = bool(config.get("require_complete_patients", True))
    reference_name = str(config.get("reference_image", modalities[0]))
    if reference_name not in modalities:
        raise ValueError(f"reference_image must be one of {modalities}: {reference_name}")

    roi_path = selected_path(patient_dir, manifest.get("roi", {}))
    if roi_path is None:
        raise ValueError("No ROI selected in manifest.")

    images = manifest.get("images", {})
    image_paths: dict[str, Path] = {}
    missing: set[str] = set()
    for modality in modalities:
        entry = images.get(modality, {}) if isinstance(images, dict) else {}
        selected = selected_path(patient_dir, entry if isinstance(entry, dict) else {})
        if selected is None:
            missing.add(modality)
        else:
            image_paths[modality] = selected

    if missing and require_complete:
        return [], [], [
            {
                "patient_id": patient_id,
                "sample_id": patient_id,
                "manifest_path": str(manifest_path),
                "error": f"missing required modalities: {sorted(missing)}",
            }
        ]
    if reference_name not in image_paths:
        raise ValueError(f"reference_image {reference_name} is missing; cannot create target grid.")

    patient_work = work_root / "patients" / safe_name(patient_id)
    resampled_dir = patient_work / "resampled"
    cache_dir = patient_work / "feature_cache"
    resampled_images, feature_roi, geometry = prepare_feature_images(
        image_paths=image_paths,
        roi_path=roi_path,
        reference_name=reference_name,
        output_dir=resampled_dir,
        config=config,
    )

    for modality in missing:
        zero_path = resampled_dir / f"{modality}_missing_zero_target_grid.nii.gz"
        if not zero_path.exists():
            create_zero_like(resampled_images[reference_name], zero_path)
        resampled_images[modality] = zero_path

    write_generic_rows(patient_work / "geometry_check.csv", geometry)

    original_metadata = {
        "is_augmented": False,
        "augmentation_index": -1,
        "rotation_x_deg": 0.0,
        "rotation_y_deg": 0.0,
        "rotation_z_deg": 0.0,
        "translation_x_mm": 0.0,
        "translation_y_mm": 0.0,
        "translation_z_mm": 0.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
    }
    original_rows = extract_sample_with_cache(
        sample_id=patient_id,
        source_subject_id=patient_id,
        original_images=image_paths,
        feature_images=resampled_images,
        roi_path=roi_path,
        feature_roi=feature_roi,
        manifest_path=manifest_path,
        config=config,
        sample_metadata=original_metadata,
        cache_dir=cache_dir,
        missing_modalities=missing,
        show_progress=show_progress,
    )

    all_rows = list(original_rows)
    augmentation = get_augmentation_config(config)
    if augmentation["enabled"]:
        seed = int(augmentation["random_state"]) + sum(ord(char) for char in patient_id)
        rng = np.random.default_rng(seed)
        for augmentation_index in range(augmentation["samples_per_patient"]):
            sample_id = f"{patient_id}-aug{augmentation_index:04d}"
            parameters = sample_augmentation_parameters(rng, augmentation, augmentation_index)
            augmented_images, augmented_roi = create_augmented_sample(
                resampled_images=resampled_images,
                resampled_roi=feature_roi,
                reference_name=reference_name,
                output_dir=patient_work / "augmented" / safe_name(sample_id),
                sample_id=sample_id,
                parameters=parameters,
            )
            all_rows.extend(
                extract_sample_with_cache(
                    sample_id=sample_id,
                    source_subject_id=patient_id,
                    original_images=image_paths,
                    feature_images=augmented_images,
                    roi_path=roi_path,
                    feature_roi=augmented_roi,
                    manifest_path=manifest_path,
                    config=config,
                    sample_metadata=parameters,
                    cache_dir=cache_dir,
                    missing_modalities=missing,
                    show_progress=show_progress,
                )
            )
    return original_rows, all_rows, []


def process_manifest_job(arguments: tuple[Path, dict[str, Any], Path, bool]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    manifest_path, config, work_root, show_progress = arguments
    try:
        return process_manifest(
            manifest_path=manifest_path,
            config=config,
            work_root=work_root,
            show_progress=show_progress,
        )
    except Exception as exc:
        return [], [], [
            {
                "patient_id": manifest_path.stem,
                "sample_id": manifest_path.stem,
                "manifest_path": str(manifest_path),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        ]


def main() -> int:
    args = parse_args()
    manifest_dir = args.manifest_dir.resolve()
    output_csv = args.output_csv.resolve()
    config_path = args.config.resolve()
    work_root = args.work_dir.resolve() if args.work_dir else output_csv.parent.resolve() / "work"
    augmented_output = (
        args.augmented_output_csv.resolve()
        if args.augmented_output_csv
        else output_csv.with_name(output_csv.stem + "_augmented.csv")
    )
    errors_csv = args.errors_csv.resolve() if args.errors_csv else output_csv.with_name(output_csv.stem + "_errors.csv")

    if not manifest_dir.is_dir():
        print(f"Manifest directory not found: {manifest_dir}", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
        import_pyfe_pyrad()
    except Exception as exc:
        print(f"extract_radiomics_features.py: {exc}", file=sys.stderr)
        return 1

    original_rows: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    paths = manifest_paths(manifest_dir)
    jobs = max(1, int(args.jobs))

    if jobs == 1:
        progress = tqdm(
            paths,
            desc="Patient manifests",
            unit="patient",
            dynamic_ncols=True,
        )
        for manifest_path in progress:
            progress.set_postfix_str(manifest_path.stem, refresh=True)
            patient_original, patient_all, patient_errors = process_manifest_job(
                (manifest_path, config, work_root, True)
            )
            original_rows.extend(patient_original)
            all_rows.extend(patient_all)
            errors.extend(patient_errors)
    else:
        print(f"Processing {len(paths)} patient manifests with {jobs} jobs.")
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(process_manifest_job, (manifest_path, config, work_root, False)): manifest_path
                for manifest_path in paths
            }
            progress = tqdm(
                as_completed(futures.keys()),
                total=len(futures),
                desc="Patient manifests",
                unit="patient",
                dynamic_ncols=True,
            )
            for future in progress:
                manifest_path = futures[future]
                patient_original, patient_all, patient_errors = future.result()
                original_rows.extend(patient_original)
                all_rows.extend(patient_all)
                errors.extend(patient_errors)
                progress.set_postfix(
                    patient=manifest_path.stem,
                    rows=len(original_rows),
                    errors=len(errors),
                    refresh=True,
                )

    if original_rows:
        write_rows(output_csv, original_rows)
    if all_rows:
        write_rows(augmented_output, all_rows)
    if errors:
        write_errors(errors_csv, errors)
    elif errors_csv.exists():
        errors_csv.unlink()

    print(
        f"Extracted original rows={len(original_rows)}, all rows={len(all_rows)} -> {output_csv}"
    )
    if errors:
        print(f"Errors: {errors_csv}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-codex")
    raise SystemExit(main())
