#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: ./run_radiomics_features.sh [manifest_dir] [output_dir] [config_path] [jobs]"
  echo
  echo "Default manifest_dir: radiomics_manifests"
  echo "Default output_dir: radiomics_features"
  echo "Default config_path: radiomics_config.yaml next to this script"
  echo "Default jobs: 1, or set RADIOMICS_JOBS"
  exit 0
fi

MANIFEST_DIR="${1:-radiomics_manifests}"
OUTPUT_DIR="${2:-radiomics_features}"
CONFIG_PATH="${3:-${SCRIPT_DIR}/radiomics_config.yaml}"
JOBS="${4:-${RADIOMICS_JOBS:-1}}"
PYTHON="${PYTHON:-python3}"

LONG_CSV="${OUTPUT_DIR}/radiomics_features_long.csv"
LONG_AUGMENTED_CSV="${OUTPUT_DIR}/radiomics_features_long_augmented.csv"
WIDE_CSV="${OUTPUT_DIR}/radiomics_features_wide.csv"
WIDE_AUGMENTED_CSV="${OUTPUT_DIR}/radiomics_features_wide_augmented.csv"
WORK_DIR="${OUTPUT_DIR}/work"

mkdir -p "${OUTPUT_DIR}"

"${PYTHON}" "${SCRIPT_DIR}/extract_radiomics_features.py" \
  "${MANIFEST_DIR}" \
  "${LONG_CSV}" \
  --config "${CONFIG_PATH}" \
  --work-dir "${WORK_DIR}" \
  --augmented-output-csv "${LONG_AUGMENTED_CSV}" \
  --jobs "${JOBS}"

"${PYTHON}" "${SCRIPT_DIR}/concatenate_radiomics_features.py" \
  "${LONG_CSV}" \
  "${WIDE_CSV}" \
  --config "${CONFIG_PATH}"

"${PYTHON}" "${SCRIPT_DIR}/concatenate_radiomics_features.py" \
  "${LONG_AUGMENTED_CSV}" \
  "${WIDE_AUGMENTED_CSV}" \
  --config "${CONFIG_PATH}"

echo "Long features: ${LONG_CSV}"
echo "Long features including augmentation: ${LONG_AUGMENTED_CSV}"
echo "Wide features: ${WIDE_CSV}"
echo "Wide features including augmentation: ${WIDE_AUGMENTED_CSV}"
