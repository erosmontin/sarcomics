#!/usr/bin/env bash
set -Eeuo pipefail

# Usage:
#   ./install_and_run_in_container.sh
#   ./install_and_run_in_container.sh radiomics_config.yaml
#   ./install_and_run_in_container.sh radiomics_config.yaml --dry-run
#
# This script is intended to run INSIDE a live Ubuntu Docker container
# with the project mounted at /workspace.

PROJECT_DIR="${PROJECT_DIR:-/workspace}"
CONFIG_FILE="${1:-radiomics_config.yaml}"
RUN_MODE="${2:-full}"

CONDA_DIR="${CONDA_DIR:-/opt/conda}"
ENV_NAME="${ENV_NAME:-able}"
PYTHON_VERSION="${PYTHON_VERSION:-3.9}"

log() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    echo
    echo "Installation or pipeline execution failed with code: $exit_code" >&2
    echo "Review the messages above to identify the failing command." >&2
    exit "$exit_code"
}

trap on_error ERR

cd "$PROJECT_DIR"

if [[ ! -f "$CONFIG_FILE" ]]; then
    fail "Configuration file not found: $PROJECT_DIR/$CONFIG_FILE"
fi

if [[ ! -f requirements.txt ]]; then
    fail "requirements.txt was not found in $PROJECT_DIR"
fi

if [[ ! -f requirements-pyfe.txt ]]; then
    fail "requirements-pyfe.txt was not found in $PROJECT_DIR"
fi

if [[ ! -f run_radiomics_pipeline.sh ]]; then
    fail "run_radiomics_pipeline.sh was not found in $PROJECT_DIR"
fi

log "1. Project information"

echo "Project directory: $PROJECT_DIR"
echo "Configuration:     $CONFIG_FILE"
echo "Run mode:          $RUN_MODE"
echo "Architecture:      $(uname -m)"

log "2. Installing Ubuntu system dependencies"

export DEBIAN_FRONTEND=noninteractive

apt-get update

apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    bzip2 \
    ca-certificates \
    cmake \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    pkg-config \
    wget

log "3. Installing Miniforge"

if [[ ! -x "$CONDA_DIR/bin/conda" ]]; then
    case "$(uname -m)" in
        x86_64|amd64)
            MINIFORGE_ARCH="x86_64"
            ;;
        aarch64|arm64)
            MINIFORGE_ARCH="aarch64"
            ;;
        *)
            fail "Unsupported architecture: $(uname -m)"
            ;;
    esac

    MINIFORGE_INSTALLER="/tmp/Miniforge3-Linux-${MINIFORGE_ARCH}.sh"

    curl -L \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${MINIFORGE_ARCH}.sh" \
        -o "$MINIFORGE_INSTALLER"

    bash "$MINIFORGE_INSTALLER" -b -p "$CONDA_DIR"
    rm -f "$MINIFORGE_INSTALLER"
else
    echo "Miniforge is already installed at $CONDA_DIR"
fi

# Make conda available in this non-interactive shell.
source "$CONDA_DIR/etc/profile.d/conda.sh"

conda --version

log "4. Creating the Python ${PYTHON_VERSION} environment"

if conda env list | awk '{print $1}' | grep -Fxq "$ENV_NAME"; then
    echo "Conda environment '$ENV_NAME' already exists."
else
    conda create \
        -n "$ENV_NAME" \
        "python=$PYTHON_VERSION" \
        pip \
        git \
        -y
fi

conda activate "$ENV_NAME"

echo "Python executable: $(which python)"
python --version

log "5. Installing Python packaging tools"

python -m pip install --upgrade \
    pip \
    setuptools \
    wheel

log "6. Installing NumPy and Cython"

python -m pip install \
    "numpy>=1.23,<2.0" \
    "Cython<3"

log "7. Installing PyRadiomics"

python -m pip install \
    --no-build-isolation \
    "PyRadiomics==3.0.1"

log "8. Installing the main requirements"

python -m pip install -r requirements.txt

log "9. Installing the PyFE requirements"

python -m pip install \
    --ignore-requires-python \
    --no-deps \
    -r requirements-pyfe.txt

log "10. Verifying the environment"

python - <<'PY'
import sys

import SimpleITK
import radiomics
import pyfe
import pyable

print("Python:", sys.version)
print("Python executable:", sys.executable)
print("PyRadiomics:", getattr(radiomics, "__version__", "unknown"))
print("SimpleITK:", SimpleITK.Version())
print("pyfe import: OK")
print("pyable import: OK")
print("Environment verification passed.")
PY

log "11. Checking dependency consistency"

# pip check may identify intentional metadata issues in the PyFE stack.
# Keep it visible, but do not hide its result.
python -m pip check || {
    echo
    echo "WARNING: pip check reported dependency metadata conflicts."
    echo "The pipeline test will continue so that runtime compatibility can be evaluated."
}

log "12. Preparing pipeline scripts"

chmod +x \
    run_radiomics_pipeline.sh \
    stage1_build_manifests.sh \
    stage2_extract_features.sh \
    stage3_qc_features.sh

export PYTHON="$CONDA_DIR/envs/$ENV_NAME/bin/python"

echo "Pipeline Python: $PYTHON"
"$PYTHON" --version

log "13. Checking input directory"

"$PYTHON" - "$CONFIG_FILE" <<'PY'
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1]).resolve()

with config_path.open("r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream)

pipeline = config.get("pipeline", {})
images_root = Path(pipeline.get("images_root", "1_3D_images"))

if not images_root.is_absolute():
    images_root = config_path.parent / images_root

images_root = images_root.resolve()

print("Configuration:", config_path)
print("Images root:", images_root)

if not images_root.is_dir():
    raise SystemExit(f"Input image directory does not exist: {images_root}")

patient_directories = sorted(
    path for path in images_root.iterdir()
    if path.is_dir()
)

print("Patient directories found:", len(patient_directories))

if not patient_directories:
    raise SystemExit(
        f"No patient directories were found inside: {images_root}"
    )

for patient_dir in patient_directories[:10]:
    print("  -", patient_dir.name)

if len(patient_directories) > 10:
    print(f"  ... and {len(patient_directories) - 10} more")
PY

log "14. Running the radiomics pipeline"

if [[ "$RUN_MODE" == "--dry-run" || "$RUN_MODE" == "dry-run" ]]; then
    ./run_radiomics_pipeline.sh "$CONFIG_FILE" --dry-run
elif [[ "$RUN_MODE" == "full" ]]; then
    ./run_radiomics_pipeline.sh "$CONFIG_FILE"
else
    fail "Unknown run mode '$RUN_MODE'. Use 'full' or '--dry-run'."
fi

log "15. Locating generated outputs"

"$PYTHON" - "$CONFIG_FILE" <<'PY'
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1]).resolve()

with config_path.open("r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream)

pipeline = config.get("pipeline", {})

manifest_dir = Path(
    pipeline.get("manifest_dir", "radiomics_manifests")
)
output_dir = Path(
    pipeline.get("output_dir", "radiomics_features")
)

if not manifest_dir.is_absolute():
    manifest_dir = config_path.parent / manifest_dir

if not output_dir.is_absolute():
    output_dir = config_path.parent / output_dir

manifest_dir = manifest_dir.resolve()
output_dir = output_dir.resolve()

print("Manifest directory:", manifest_dir)
print("Output directory:", output_dir)

if manifest_dir.exists():
    manifests = list(manifest_dir.glob("*.json"))
    print("JSON manifest files:", len(manifests))
else:
    print("Manifest directory was not created.")

if output_dir.exists():
    output_files = sorted(
        path for path in output_dir.rglob("*")
        if path.is_file()
    )

    print("Generated files:", len(output_files))

    for path in output_files[:30]:
        print("  -", path.relative_to(output_dir))

    if len(output_files) > 30:
        print(f"  ... and {len(output_files) - 30} more")

    main_output = output_dir / "radiomics_features_wide.csv"

    if main_output.exists():
        print()
        print("Main feature table:")
        print(main_output)
        print("Size:", main_output.stat().st_size, "bytes")
else:
    print("Output directory was not created.")
PY

log "16. Restoring host ownership"

# HOST_UID and HOST_GID can be passed when starting Docker.
if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
    "$PYTHON" - "$CONFIG_FILE" "$HOST_UID" "$HOST_GID" <<'PY'
import os
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1]).resolve()
host_uid = int(sys.argv[2])
host_gid = int(sys.argv[3])

with config_path.open("r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream)

pipeline = config.get("pipeline", {})

paths = [
    Path(pipeline.get("manifest_dir", "radiomics_manifests")),
    Path(pipeline.get("output_dir", "radiomics_features")),
]

for path in paths:
    if not path.is_absolute():
        path = config_path.parent / path

    path = path.resolve()

    if not path.exists():
        continue

    for root, directories, files in os.walk(path):
        os.chown(root, host_uid, host_gid)

        for name in directories:
            os.chown(Path(root) / name, host_uid, host_gid)

        for name in files:
            os.chown(Path(root) / name, host_uid, host_gid)

    print("Updated ownership:", path)
PY
else
    echo "HOST_UID or HOST_GID was not provided."
    echo "Generated files may be owned by root on the host."
fi

log "Radiomics clean-install test completed successfully"