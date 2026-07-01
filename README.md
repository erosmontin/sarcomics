# Radiomics Feature Extraction

Pipeline to scan patient directories, build JSON manifests, and extract PyFE/PyRadiomics features into CSV files. Supports four MRI modalities: **T1w, T1w contrast-enhanced (T1wC), T2w, and ADC**.

---

## Environment Setup

This project expects a Python 3.9 conda environment. The examples below use an
environment named `able`.

### 1. Install Miniconda

If `conda` is not installed, install Miniconda first.

Linux x86_64:

```bash
cd /tmp
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p "$HOME/miniconda3"
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda init bash
```

macOS:

```bash
cd /tmp
# Apple Silicon:
curl -O https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh
bash Miniconda3-latest-MacOSX-arm64.sh -b -p "$HOME/miniconda3"

# Intel Mac users should download the x86_64 installer instead:
# https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh

source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda init zsh
```

After `conda init`, open a new terminal or run:

```bash
source ~/.bashrc   # Linux/bash
# or
source ~/.zshrc    # macOS/zsh
```

### 2. Create the `able` Environment

From the repository root:

```bash
conda create -n able python=3.9 pip git -y
conda activate able
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The requirements install PyRadiomics, SimpleITK, PyFE, pyable, and the other
feature-extraction dependencies.

### 3. Verify the Environment

```bash
python -c "import SimpleITK, radiomics, pyfe, pyable; print('environment ok')"
```

All scripts respect the `PYTHON` environment variable. This is useful when
running from outside an activated conda shell:

```bash
export PYTHON="$HOME/miniconda3/envs/able/bin/python"
```

---

## Stage 1 — Build Manifests

Scans `1_3D_images/` (one sub-directory per patient) and writes one JSON manifest per patient using the filename patterns in `radiomics_config.yaml`.

```bash
./stage1_build_manifests.sh
```

Arguments (all optional, shown with defaults):

```bash
./stage1_build_manifests.sh 1_3D_images radiomics_manifests radiomics_config.yaml
```

Outputs:
- `radiomics_manifests/<patient_id>.json` — one manifest per patient
- `radiomics_manifests/summary.json` — aggregated table of what was found

---

## Stage 2 — Extract Radiomics Features

```bash
./stage2_extract_features.sh radiomics_manifests radiomics_features radiomics_config.yaml 4
```

Arguments:

| Position | Default | Description |
|---|---|---|
| 1 | `radiomics_manifests` | Directory of JSON manifests from Stage 1 |
| 2 | `radiomics_features` | Output directory |
| 3 | `radiomics_config.yaml` | Configuration file |
| 4 | `1` | Number of parallel patient jobs |

Outputs:
- `radiomics_features/radiomics_features_long.csv` — one row per patient × modality (original images only)
- `radiomics_features/radiomics_features_long_augmented.csv` — same, including augmented samples
- `radiomics_features/radiomics_features_wide.csv` — one row per patient (all modalities concatenated)
- `radiomics_features/radiomics_features_wide_augmented.csv` — wide table including augmented samples
- `radiomics_features/work/patients/<id>/qc/` — ROI overlay PNG images for quality control
- `radiomics_features/radiomics_features_long_errors.csv` — patients that failed (when present)

---

## Configuration (`radiomics_config.yaml`)

### Modalities

The pipeline expects four image types per patient. Edit `modalities` to match your filenames:

```yaml
modalities:
  t1w:
    required: true
    patterns: ["T1w.mha", "*T1*.mha"]
    exclude_patterns: ["*CONT*", "*contrast*"]
  t1wc:
    required: true
    patterns: ["T1w_CONT.mha", "*T1*CONT*.mha"]
  t2w:
    required: true
    patterns: ["T2w.mha", "*T2*.mha"]
  adc:
    required: true
    patterns: ["ADC.mha", "*ADC*.mha"]
```

### ROI

```yaml
roi:
  patterns: ["ROI_T.nrrd", "*ROI*T*.nrrd"]
```

### Key extraction settings

```yaml
working_spacing_mm: [2.0, 2.0, 2.0]   # All images resampled to this isotropic resolution
reference_image: t1w                   # Defines the target spatial grid
bin_counts: [16]                       # PyRadiomics histogram bins
pyfe_radius: [1]                       # GLCM distances
normalize: true                        # PyRadiomics intensity normalization
image_types:
  - all                                # Use [Original] for speed; all = 10 filter types
intensity_scaling:
  adc:
    divide_by: 3000.0                  # ADC pre-scaled by water diffusivity reference
require_complete_patients: false       # false = keep patients with missing modalities (NaN features)
```

### Missing modalities

When `require_complete_patients: false` (default), patients with missing image files are still processed. Features for the missing modality are set to `nan` and `feature_status` is set to `missing_modality` in the output CSV. All other modalities are extracted normally.

### Feature cache

```yaml
feature_cache:
  enabled: true           # Skip re-extraction if inputs are unchanged
  hash_file_contents: false  # true = slower but detects content changes without mtime
```

### QC overlays

```yaml
qc:
  enabled: true    # Save ROI-on-image PNG per modality per patient
  alpha: 0.45
  dpi: 120
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Missing required package` on startup | Dependencies not installed | `pip install -r requirements.txt` in the `able` environment |
| Patient in `_errors.csv` with `missing required modalities` | `require_complete_patients: true` and a file is absent | Set `require_complete_patients: false` or add the missing file |
| All features are `nan` | Wrong ROI label | Check `radiomics.label` in config matches your mask value |
| Slow extraction | Many image types / large images | Set `image_types: [Original]` or reduce `bin_counts` |
| Wrong files matched | Filename patterns too broad | Refine `patterns`/`exclude_patterns` in `modalities` section |
| Cache not reused | File timestamps changed (e.g., re-copy) | Set `feature_cache.hash_file_contents: true` |




## Cite Us
1. Montin, E., Kijowski, R., Youm, T., & Lattanzi, R. (2024). Radiomics features outperform standard radiological measurements in detecting femoroacetabular impingement on three‐dimensional magnetic resonance imaging. In Journal of Orthopaedic Research. Wiley. https://doi.org/10.1002/jor.25952

1. Montin, E., Kijowski, R., Youm, T., & Lattanzi, R. (2023). A radiomics approach to the diagnosis of femoroacetabular impingement. In Frontiers in Radiology (Vol. 3). Frontiers Media SA. https://doi.org/10.3389/fradi.2023.1151258

1. Cavatorta, C., Meroni, S., Montin, E., Oprandi, M. C., Pecori, E., Lecchi, M., Diletto, B., Alessandro, O., Peruzzo, D., Biassoni, V., Schiavello, E., Bologna, M., Massimino, M., Poggi, G., Mainardi, L., Arrigoni, F., Spreafico, F., Verderio, P., Pignoli, E., & Gandola, L. (2021). Retrospective study of late radiation-induced damages after focal radiotherapy for childhood brain tumors. In S. D. Ginsberg (Ed.), PLOS ONE (Vol. 16, Issue 2, p. e0247748). Public Library of Science (PLoS). https://doi.org/10.1371/journal.pone.0247748

1. Montin, E., Belfatto, A., Bologna, M., Meroni, S., Cavatorta, C., Pecori, E., Diletto, B., Massimino, M., Oprandi, M. C., Poggi, G., Arrigoni, F., Peruzzo, D., Pignoli, E., Gandola, L., Cerveri, P., & Mainardi, L. (2020). A multi-metric registration strategy for the alignment of longitudinal brain images in pediatric oncology. In Medical &amp; Biological Engineering &amp; Computing (Vol. 58, Issue 4, pp. 843–855). Springer Science and Business Media LLC. https://doi.org/10.1007/s11517-019-02109-4




[*Dr. Eros Montin, PhD*](https://biodimensional.com)
**46&2 just ahead of me!**