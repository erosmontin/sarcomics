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
python -m pip install "numpy>=1.23,<2.0" "Cython<3"
python -m pip install --no-build-isolation "PyRadiomics==3.0.1"
python -m pip install -r requirements.txt
python -m pip install --ignore-requires-python --no-deps -r requirements-pyfe.txt
```

The requirements install PyRadiomics, SimpleITK, PyFE, pyable, and the other
feature-extraction dependencies. PyRadiomics is installed once before the full
requirements because its source build expects `numpy` to already be importable.
The `--ignore-requires-python` flag is used only for the PyFE Git stack because
PyFE rejects Python 3.9 patch releases such as 3.9.25 even though the pipeline
is intentionally using Python 3.9. Do not use these instructions with Python
3.10 or newer.

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

## Docker Clean-Install Test

Docker is useful when you want to test the pipeline as if it were running on a
new machine. The image installs Python 3.9, system build tools, everything in
`requirements.txt`, and the PyFE Git stack from `requirements-pyfe.txt`.

Build the image from the repository root:

```bash
docker build -t radiomics-pipeline:py39 .
```

Check that the main packages installed:

```bash
docker run --rm radiomics-pipeline:py39 \
  python -c "import SimpleITK, radiomics, pyfe, pyable; print('environment ok')"
```

Run the pipeline dry run from inside Docker. The project directory is mounted as
`/workspace`, so the container reads the same `radiomics_config.yaml` and writes
outputs back to the same host directory.

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD":/workspace \
  -w /workspace \
  radiomics-pipeline:py39 \
  /app/run_radiomics_pipeline.sh /workspace/radiomics_config.yaml --dry-run
```

Run the full pipeline:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD":/workspace \
  -w /workspace \
  radiomics-pipeline:py39 \
  /app/run_radiomics_pipeline.sh /workspace/radiomics_config.yaml
```

For a truly fresh full run, set `pipeline.manifest_dir` and
`pipeline.output_dir` in `radiomics_config.yaml` to new empty directories before
starting Docker. Keep `pipeline.images_root` pointed at the patient image
directory.

On macOS or Windows Docker Desktop, replace `$(id -u):$(id -g)` with your local
user setting if needed, or omit the `--user` line and fix file ownership after
the test.

---

## One-Command Pipeline

For routine hospital use, edit `radiomics_config.yaml` and run one command:

```bash
./run_radiomics_pipeline.sh
```

The default config path is `radiomics_config.yaml` next to the script. To use a
different config:

```bash
./run_radiomics_pipeline.sh my_config.yaml
```

Fast dry run, useful before starting a long extraction:

```bash
./run_radiomics_pipeline.sh --dry-run
```

Pipeline paths and parallel jobs live in one place:

```yaml
pipeline:
  images_root: "1_3D_images"
  manifest_dir: "radiomics_manifests"
  output_dir: "radiomics_features"
  jobs: 4
```

The runner uses `augmentation.enabled` to choose the normal Stage 2 behavior:

```yaml
augmentation:
  enabled: false  # original-only
```

Set `augmentation.enabled: true` when you want the original wide table plus a
separate augmented-only wide table. For a special rerun of augmented samples
only, keep `augmentation.enabled: true` and use:

```bash
./run_radiomics_pipeline.sh --mode augmentation-only
```

The one-command runner executes:

```bash
./stage1_build_manifests.sh ...
./stage2_extract_features.sh ...
./stage3_qc_features.sh ...
```

Final user-facing outputs are written to `pipeline.output_dir`:

```text
radiomics_features_wide.csv
  original patient feature table; rows = number of patients

radiomics_features_wide_augmented.csv
  augmented feature table only, when augmentation.enabled is true

qc_features/
  QC counts and all wide feature values for review
```

---

## Prepare Input Images from DICOM

The radiomics pipeline does not read DICOM folders directly. First convert each
MRI series into one 3D image file, then place those files into one folder per
patient.

### Expected Directory Layout

Use this layout before running Stage 1:

```text
1_3D_images/
  AR19/
    T1w.mha
    T1w_CONT.mha
    T2w.mha
    ADC.mha
    ROI_T.nrrd
  AR20/
    T1w.mha
    T1w_CONT.mha
    T2w.mha
    ADC.mha
    ROI_T.nrrd
```

The patient folder name, such as `AR19`, becomes the `patient_id` in the output.

### Required Image Names

The default config expects these files:

| Modality | Preferred filename | Meaning |
|---|---|---|
| `t1w` | `T1w.mha` | T1-weighted pre-contrast image |
| `t1wc` | `T1w_CONT.mha` | T1-weighted post-contrast image |
| `t2w` | `T2w.mha` | T2-weighted image |
| `adc` | `ADC.mha` | ADC map |
| ROI | `ROI_T.nrrd` | Tumor/target mask; label value defaults to `1` |

The matcher is flexible: `T1w_CONT.mha`, names containing `T1` plus `CONT`,
`contrast`, or `post`, and `.nrrd` variants are also accepted. Keep the
pre-contrast T1 filename free of `CONT`, `contrast`, and `post`, because those
tokens are reserved for `t1wc`.

Extra files such as `DWI_B0.mha` or `DWI_B800.mha` can stay in the patient
folder; they are ignored unless you add them to `modalities` in
`radiomics_config.yaml`.

### Convert a DICOM Series to `.mha`

Each image file should come from exactly one DICOM series. If your DICOM export
already has one folder per sequence, conversion is straightforward.

Example input:

```text
DICOM/
  AR19/
    T1w/
    T1w_CONT/
    T2w/
    ADC/
```

Convert one series with SimpleITK:

```bash
conda activate able
python - <<'PY'
from pathlib import Path
import SimpleITK as sitk

series_dir = Path("DICOM/AR19/T1w")
output_path = Path("1_3D_images/AR19/T1w.mha")

reader = sitk.ImageSeriesReader()
series_ids = reader.GetGDCMSeriesIDs(str(series_dir))
if not series_ids:
    raise SystemExit(f"No DICOM series found in {series_dir}")
if len(series_ids) > 1:
    print(f"Multiple series found; using first series: {series_ids[0]}")

dicom_files = reader.GetGDCMSeriesFileNames(str(series_dir), series_ids[0])
reader.SetFileNames(dicom_files)
image = reader.Execute()

output_path.parent.mkdir(parents=True, exist_ok=True)
sitk.WriteImage(image, str(output_path), True)
print(f"Wrote {output_path}")
PY
```

Repeat the same pattern for each modality:

```text
DICOM/AR19/T1w       -> 1_3D_images/AR19/T1w.mha
DICOM/AR19/T1w_CONT  -> 1_3D_images/AR19/T1w_CONT.mha
DICOM/AR19/T2w       -> 1_3D_images/AR19/T2w.mha
DICOM/AR19/ADC       -> 1_3D_images/AR19/ADC.mha
```

You can also use 3D Slicer, ITK-SNAP, or another medical-image tool to export
the same series as `.mha`, `.nrrd`, `.nii`, or `.nii.gz`; those extensions are
accepted by the config.

### Create or Export the ROI Mask

Create the ROI mask in the same patient space using your segmentation tool.
Save it as:

```text
1_3D_images/<patient_id>/ROI_T.nrrd
```

The mask should be a label image where the tumor/target voxels have value `1`.
If your mask uses another label value, update:

```yaml
radiomics:
  label: 1
```

in `radiomics_config.yaml`.

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

Run Stage 2 after Stage 1 has created `radiomics_manifests/*.json`:

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
| 5 | `all` | Run mode: `all`, `original-only`, or `augmentation-only` |

Outputs:
- `radiomics_features/radiomics_features_wide.csv` — final patient feature table, one row per patient
- `radiomics_features/radiomics_features_wide_augmented.csv` — augmented patient feature table, one row per augmented sample, when augmentation is enabled
- `radiomics_features/work/patients/<id>/resampled/` — intermediate resampled images
- `radiomics_features/radiomics_features_errors.csv` — patients that failed (when present)

Stage 2 uses long-format CSVs internally during extraction and concatenation,
but those files are kept under `radiomics_features/work/intermediate/`. The
hospital-facing feature tables are the wide CSVs above.

Stage 2 modes:

```bash
# Original patients only.
./stage2_extract_features.sh radiomics_manifests radiomics_features radiomics_config.yaml 4 original-only

# Original patients, plus augmented patients if augmentation.enabled is true.
./stage2_extract_features.sh radiomics_manifests radiomics_features radiomics_config.yaml 4 all

# Augmented patients only. This leaves radiomics_features_wide.csv untouched.
./stage2_extract_features.sh radiomics_manifests radiomics_features radiomics_config.yaml 4 augmentation-only
```

When augmentation is enabled, the augmented table is separate:

```text
radiomics_features/radiomics_features_wide.csv
  original patients only

radiomics_features/radiomics_features_wide_augmented.csv
  augmented samples only
```

### Typical Full Run

After DICOM conversion and ROI export, a normal run is:

```bash
conda activate able
./stage1_build_manifests.sh 1_3D_images radiomics_manifests radiomics_config.yaml
./stage2_extract_features.sh radiomics_manifests radiomics_features radiomics_config.yaml 4
./stage3_qc_features.sh radiomics_features radiomics_manifests radiomics_config.yaml
```

---

## Stage 3 — QC Feature Review

Stage 3 is a QC review step for clinicians and data managers. It checks the
final wide tables only. It does not calculate correlations and it does not
require the long-format intermediate tables.

```bash
./stage3_qc_features.sh radiomics_features radiomics_manifests radiomics_config.yaml
```

Outputs:
- `radiomics_features/qc_features/01_qc_summary.csv` — expected vs observed row and feature counts
- `radiomics_features/qc_features/02_patient_row_counts.csv` — row count check per patient
- `radiomics_features/qc_features/03_modality_feature_counts.csv` — feature column count per modality
- `radiomics_features/qc_features/04_all_feature_columns.csv` — every feature column with missingness and numeric summary
- `radiomics_features/qc_features/05_all_feature_values_wide.csv` — all patient-level wide feature values
- `radiomics_features/qc_features/06_all_feature_values_wide_augmented.csv` — all augmented wide feature values, when present
- `radiomics_features/qc_features/07_stage2_errors.csv` — copied Stage 2 errors, when present

Options:

```bash
./stage3_qc_features.sh [features_dir] [manifest_dir] [config_path]
```

The final product for downstream analysis is the Stage 2 output directory,
especially `radiomics_features_wide.csv`. Stage 3 helps confirm that this
patient-level feature table has the expected number of patient rows and feature
columns before it is shared or analyzed.

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
| `Missing required package` on startup | Dependencies not installed | Re-run the two install commands for `requirements.txt` and `requirements-pyfe.txt` in the `able` environment |
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
