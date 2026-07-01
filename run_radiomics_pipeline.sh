#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
CONFIG_PATH="${SCRIPT_DIR}/radiomics_config.yaml"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./run_radiomics_pipeline.sh [config_path] [--dry-run] [--mode MODE]

Runs the full radiomics pipeline:
  Stage 1: build manifests
  Stage 2: extract and concatenate wide feature tables
  Stage 3: QC feature counts and patient rows

All pipeline paths are read from radiomics_config.yaml:
  pipeline.images_root
  pipeline.manifest_dir
  pipeline.output_dir
  pipeline.jobs

The Stage 2 mode is inferred from augmentation.enabled:
  false -> original-only
  true  -> all

Use --mode only for an explicit rerun:
  all, original-only, augmentation-only

Default config_path: radiomics_config.yaml next to this script
EOF
}

MODE_OVERRIDE=""

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --mode)
      shift
      if [[ "$#" -eq 0 ]]; then
        echo "--mode requires a value: all, original-only, or augmentation-only" >&2
        exit 2
      fi
      if [[ -n "${MODE_OVERRIDE}" ]]; then
        echo "Stage 2 mode was provided more than once." >&2
        exit 2
      fi
      MODE_OVERRIDE="$1"
      ;;
    --mode=)
      echo "--mode requires a value: all, original-only, or augmentation-only" >&2
      exit 2
      ;;
    --mode=*)
      if [[ -n "${MODE_OVERRIDE}" ]]; then
        echo "Stage 2 mode was provided more than once." >&2
        exit 2
      fi
      MODE_OVERRIDE="${1#--mode=}"
      ;;
    *)
      CONFIG_PATH="$1"
      ;;
  esac
  shift
done

CONFIG_PATH="$("${PYTHON}" - "${CONFIG_PATH}" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config file not found: ${CONFIG_PATH}" >&2
  exit 2
fi
CONFIG_DIR="$(cd "$(dirname "${CONFIG_PATH}")" && pwd)"

resolve_config_path() {
  local value="$1"
  if [[ "${value}" = /* ]]; then
    echo "${value}"
  else
    echo "${CONFIG_DIR}/${value}"
  fi
}

read_config() {
  local key="$1"
  local default="$2"
  "${PYTHON}" - "${CONFIG_PATH}" "${key}" "${default}" <<'PY'
from pathlib import Path
import sys

import yaml

config_path = Path(sys.argv[1])
key = sys.argv[2]
default = sys.argv[3]

with config_path.open("r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream) or {}

value = config
for part in key.split("."):
    if isinstance(value, dict) and part in value:
        value = value[part]
    else:
        value = default
        break

if isinstance(value, bool):
    print(str(value).lower())
elif value is None:
    print(default)
else:
    print(value)
PY
}

require_config() {
  local key="$1"
  local value
  value="$(read_config "${key}" "__MISSING__")"
  if [[ "${value}" == "__MISSING__" || -z "${value}" ]]; then
    echo "Missing required config key: ${key}" >&2
    exit 2
  fi
  echo "${value}"
}

IMAGES_ROOT="$(resolve_config_path "$(require_config pipeline.images_root)")"
MANIFEST_DIR="$(resolve_config_path "$(require_config pipeline.manifest_dir)")"
OUTPUT_DIR="$(resolve_config_path "$(require_config pipeline.output_dir)")"
JOBS="$(require_config pipeline.jobs)"
AUGMENTATION_ENABLED="$(read_config augmentation.enabled false)"

if [[ -n "${MODE_OVERRIDE}" ]]; then
  MODE="${MODE_OVERRIDE}"
else
  if [[ "${AUGMENTATION_ENABLED}" == "true" ]]; then
    MODE="all"
  else
    MODE="original-only"
  fi
fi

case "${MODE}" in
  all|original-only|augmentation-only)
    ;;
  *)
    echo "Invalid Stage 2 mode: ${MODE}. Allowed: all, original-only, augmentation-only" >&2
    exit 2
    ;;
esac

if [[ "${MODE}" == "augmentation-only" && "${AUGMENTATION_ENABLED}" != "true" ]]; then
  echo "augmentation-only requires augmentation.enabled: true in ${CONFIG_PATH}" >&2
  exit 2
fi

echo "Radiomics pipeline"
echo "  config:       ${CONFIG_PATH}"
echo "  images_root:  ${IMAGES_ROOT}"
echo "  manifest_dir: ${MANIFEST_DIR}"
echo "  output_dir:   ${OUTPUT_DIR}"
echo "  jobs:         ${JOBS}"
echo "  mode:         ${MODE}"
echo

run_cmd() {
  echo "+ $*"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    "$@"
  fi
}

run_cmd "${SCRIPT_DIR}/stage1_build_manifests.sh" \
  "${IMAGES_ROOT}" \
  "${MANIFEST_DIR}" \
  "${CONFIG_PATH}"

run_cmd "${SCRIPT_DIR}/stage2_extract_features.sh" \
  "${MANIFEST_DIR}" \
  "${OUTPUT_DIR}" \
  "${CONFIG_PATH}" \
  "${JOBS}" \
  "${MODE}"

run_cmd "${SCRIPT_DIR}/stage3_qc_features.sh" \
  "${OUTPUT_DIR}" \
  "${MANIFEST_DIR}" \
  "${CONFIG_PATH}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo
  echo "Dry run complete. No commands were executed."
  exit 0
fi

WIDE_CSV="${OUTPUT_DIR}/radiomics_features_wide.csv"
AUGMENTED_CSV="${OUTPUT_DIR}/radiomics_features_wide_augmented.csv"
QC_SUMMARY="${OUTPUT_DIR}/qc_features/01_qc_summary.csv"

echo
echo "Pipeline complete."
if [[ -f "${WIDE_CSV}" ]]; then
  echo "  Original wide feature table:  ${WIDE_CSV}"
fi
if [[ -f "${AUGMENTED_CSV}" ]]; then
  echo "  Augmented wide feature table: ${AUGMENTED_CSV}"
fi
if [[ -f "${QC_SUMMARY}" ]]; then
  echo "  QC summary:                    ${QC_SUMMARY}"
  "${PYTHON}" - "${QC_SUMMARY}" <<'PY'
import csv
import sys

with open(sys.argv[1], newline="", encoding="utf-8") as stream:
    rows = {row["metric"]: row["value"] for row in csv.DictReader(stream)}

for key in [
    "patient_count_from_manifests",
    "observed_wide_rows",
    "feature_columns_per_modality",
    "observed_wide_feature_columns",
    "observed_augmented_wide_rows",
    "stage2_error_rows",
]:
    if key in rows:
        print(f"  {key}: {rows[key]}")
PY
fi
