# Radiomics Feature Extraction

Small pipeline to find patient images, build JSON manifests, and extract PyFE/PyRadiomics features into CSV files.

## Install

Use a Python 3.9 environment for best PyFE/PyRadiomics compatibility.

```bash
python3 -m pip install -r requirements.txt
```

## 1. Build Manifests

This scans `1_3D_images/` and writes one JSON manifest per patient.

```bash
./run_all.sh
```

Outputs:

- `radiomics_manifests/*.json`
- `radiomics_manifests/summary.json`

## 2. Extract Radiomics

```bash
python3 extract_radiomics_features.py radiomics_manifests radiomics_features/radiomics_features_long.csv \
  --config radiomics_config.yaml \
  --work-dir radiomics_features/work \
  --jobs 4
```

Or run extraction plus wide-table concatenation:

```bash
./run_radiomics_features.sh radiomics_manifests radiomics_features radiomics_config.yaml 4
```

Outputs:

- `radiomics_features/radiomics_features_long.csv`
- `radiomics_features/radiomics_features_long_augmented.csv`
- `radiomics_features/radiomics_features_wide.csv`
- `radiomics_features/radiomics_features_wide_augmented.csv`
- ROI overlay QC PNGs under `radiomics_features/work/patients/<patient>/qc/`
- `radiomics_features/*_errors.csv` when failures occur

## Configuration

Edit `radiomics_config.yaml`.

Important fields:

- `modalities`: expected image types and filename patterns
- `roi`: ROI filename patterns
- `working_spacing_mm`: image resampling spacing before extraction
- `bin_counts`: PyRadiomics bin counts
- `pyfe_radius`: GLCM distances/radii
- `skip_image_types`: image filters to exclude; `LBP3D` is skipped by default
- `normalize`: PyRadiomics normalization
- `skip_failed_image_types`: keep extraction running if one filtered image type fails
- `intensity_scaling.adc.divide_by`: ADC pre-scaling, currently `/3000`
- `qc`: ROI overlay PNG settings
- `augmentation`: optional affine augmentation settings

ROI masks are passed to PyFE, which resamples them onto each feature image during extraction.
