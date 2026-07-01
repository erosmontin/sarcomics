#!/usr/bin/env bash
set -Eeuo pipefail

# Run this script from the radiomics repository root.
# Usage:
#   ./docker_clean_install_test.sh
#   ./docker_clean_install_test.sh radiomics_config.yaml dry-run
#   ./docker_clean_install_test.sh radiomics_config.yaml full

CONFIG_REL="${1:-radiomics_config.yaml}"
MODE="${2:-full}"

if [[ "$MODE" != "full" && "$MODE" != "dry-run" ]]; then
  echo "ERROR: mode must be 'full' or 'dry-run'." >&2
  exit 2
fi

for required in \
  "$CONFIG_REL" \
  requirements.txt \
  requirements-pyfe.txt \
  run_radiomics_pipeline.sh; do
  if [[ ! -f "$required" ]]; then
    echo "ERROR: '$required' was not found." >&2
    echo "Run this script from the repository root." >&2
    exit 2
  fi
done

REPO_ROOT="$(pwd -P)"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

DOCKER_TTY=(-i)
if [[ -t 0 && -t 1 ]]; then
  DOCKER_TTY+=(-t)
fi

echo "Repository: $REPO_ROOT"
echo "Configuration: $CONFIG_REL"
echo "Mode: $MODE"
echo "Docker test outputs will be written to:"
echo "  radiomics_manifests_docker_test/"
echo "  radiomics_features_docker_test/"
echo "  docker_clean_install_test.log"

docker run --rm --init "${DOCKER_TTY[@]}" \
  --name radiomics-clean-install-test \
  -e HOST_UID="$HOST_UID" \
  -e HOST_GID="$HOST_GID" \
  -e CONFIG_REL="$CONFIG_REL" \
  -e TEST_MODE="$MODE" \
  --mount "type=bind,src=$REPO_ROOT,dst=/workspace" \
  --workdir /workspace \
  ubuntu:22.04 \
  bash -lc '
    set -Eeuo pipefail

    export DEBIAN_FRONTEND=noninteractive
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    export PYTHONDONTWRITEBYTECODE=1
    export MPLCONFIGDIR=/tmp/matplotlib

    touch /workspace/docker_clean_install_test.log
    exec > >(tee /workspace/docker_clean_install_test.log) 2>&1

    cleanup() {
      chown -R "$HOST_UID:$HOST_GID" \
        /workspace/radiomics_manifests_docker_test \
        /workspace/radiomics_features_docker_test \
        /workspace/docker_clean_install_test.log \
        2>/dev/null || true
    }
    trap cleanup EXIT

    echo "============================================================"
    echo "1. Installing Ubuntu build dependencies"
    echo "============================================================"
    apt-get update
    apt-get install -y --no-install-recommends \
      bash \
      bzip2 \
      build-essential \
      ca-certificates \
      cmake \
      curl \
      git \
      libgl1 \
      libglib2.0-0 \
      libgomp1 \
      libsm6 \
      libxext6 \
      libxrender1 \
      pkg-config
    rm -rf /var/lib/apt/lists/*

    echo "============================================================"
    echo "2. Installing a fresh Miniforge/conda distribution"
    echo "============================================================"
    case "$(uname -m)" in
      x86_64) MINIFORGE_ARCH="x86_64" ;;
      aarch64|arm64) MINIFORGE_ARCH="aarch64" ;;
      *)
        echo "ERROR: unsupported container architecture: $(uname -m)" >&2
        exit 2
        ;;
    esac

    curl -fsSL \
      "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${MINIFORGE_ARCH}.sh" \
      -o /tmp/miniforge.sh
    bash /tmp/miniforge.sh -b -p /opt/conda

    echo "============================================================"
    echo "3. Creating a new Python 3.9 conda environment"
    echo "============================================================"
    /opt/conda/bin/conda create -n able python=3.9 pip git -y

    PYTHON_BIN=/opt/conda/envs/able/bin/python
    export PYTHON="$PYTHON_BIN"

    "$PYTHON_BIN" --version
    "$PYTHON_BIN" -m pip --version

    echo "============================================================"
    echo "4. Installing the project dependencies from scratch"
    echo "============================================================"
    "$PYTHON_BIN" -m pip install --upgrade pip setuptools wheel
    "$PYTHON_BIN" -m pip install "numpy>=1.23,<2.0" "Cython<3"
    "$PYTHON_BIN" -m pip install --no-build-isolation "PyRadiomics==3.0.1"
    "$PYTHON_BIN" -m pip install -r requirements.txt
    "$PYTHON_BIN" -m pip install \
      --ignore-requires-python \
      --no-deps \
      -r requirements-pyfe.txt

    echo "============================================================"
    echo "5. Verifying imports and dependency consistency"
    echo "============================================================"
    "$PYTHON_BIN" -c \
      "import SimpleITK, radiomics, pyfe, pyable; print(\"environment ok\")"
    "$PYTHON_BIN" -m pip check

    echo "============================================================"
    echo "6. Validating the YAML and creating an isolated test config"
    echo "============================================================"
    rm -rf \
      /workspace/radiomics_manifests_docker_test \
      /workspace/radiomics_features_docker_test

    "$PYTHON_BIN" - <<"PY"
from pathlib import Path
import os
import sys
import yaml

source = Path("/workspace") / os.environ["CONFIG_REL"]
target = Path("/tmp/radiomics_config.docker.yaml")

with source.open("r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

if not isinstance(config, dict):
    raise SystemExit("ERROR: the YAML root must be a mapping.")

problems = []
for modality, values in (config.get("modalities") or {}).items():
    for index, pattern in enumerate((values or {}).get("patterns") or []):
        if pattern is None:
            problems.append(
                f"modalities.{modality}.patterns[{index}] is null; "
                "remove the empty dash from the YAML"
            )

for index, pattern in enumerate(((config.get("roi") or {}).get("patterns") or [])):
    if pattern is None:
        problems.append(
            f"roi.patterns[{index}] is null; remove the empty dash from the YAML"
        )

if problems:
    print("ERROR: invalid pattern entries were found:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(2)

pipeline = config.setdefault("pipeline", {})
pipeline["manifest_dir"] = "radiomics_manifests_docker_test"
pipeline["output_dir"] = "radiomics_features_docker_test"

with target.open("w", encoding="utf-8") as f:
    yaml.safe_dump(config, f, sort_keys=False)

print(f"Docker test configuration: {target}")
print(f"Images root: {pipeline.get('images_root')}")
print(f"Jobs: {pipeline.get('jobs')}")
print(f"Image types: {config.get('image_types')}")
print(f"Augmentation enabled: {(config.get('augmentation') or {}).get('enabled')}")
PY

    echo "============================================================"
    echo "7. Running the pipeline dry run"
    echo "============================================================"
    bash ./run_radiomics_pipeline.sh \
      /tmp/radiomics_config.docker.yaml \
      --dry-run

    if [[ "$TEST_MODE" == "dry-run" ]]; then
      echo "Dry-run-only test completed successfully."
      exit 0
    fi

    echo "============================================================"
    echo "8. Running the complete radiomics pipeline"
    echo "============================================================"
    bash ./run_radiomics_pipeline.sh \
      /tmp/radiomics_config.docker.yaml

    echo "============================================================"
    echo "9. Checking the generated outputs"
    echo "============================================================"
    "$PYTHON_BIN" - <<"PY"
from pathlib import Path
import csv

output = Path("/workspace/radiomics_features_docker_test")
wide = output / "radiomics_features_wide.csv"
qc = output / "qc_features" / "01_qc_summary.csv"
errors = output / "radiomics_features_errors.csv"

missing = [str(path) for path in (wide, qc) if not path.is_file() or path.stat().st_size == 0]
if missing:
    raise SystemExit("ERROR: expected non-empty outputs were not created:\n  " + "\n  ".join(missing))

if errors.is_file() and errors.stat().st_size > 0:
    with errors.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if rows:
        print(f"WARNING: Stage 2 reported {len(rows)} failed patient(s): {errors}")
        for row in rows[:5]:
            print(row)
        raise SystemExit(3)

print(f"PASS: {wide}")
print(f"PASS: {qc}")
print("The clean-install Docker test completed successfully.")
PY
  '
