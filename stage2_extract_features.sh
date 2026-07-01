#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: ./stage2_extract_features.sh [manifest_dir] [output_dir] [config_path] [jobs] [mode]"
  echo
  echo "Extracts radiomics features and writes patient-level wide tables."
  echo
  echo "Default manifest_dir: radiomics_manifests"
  echo "Default output_dir: radiomics_features"
  echo "Default config_path: radiomics_config.yaml next to this script"
  echo "Default jobs: 1, or set RADIOMICS_JOBS"
  echo "Default mode: all"
  echo "Modes: all, original-only, augmentation-only"
  exit 0
fi

MANIFEST_DIR="${1:-radiomics_manifests}"
OUTPUT_DIR="${2:-radiomics_features}"
CONFIG_PATH="${3:-${SCRIPT_DIR}/radiomics_config.yaml}"
JOBS="${4:-${RADIOMICS_JOBS:-1}}"
MODE="${5:-${RADIOMICS_STAGE2_MODE:-all}}"
PYTHON="${PYTHON:-python3}"

WIDE_CSV="${OUTPUT_DIR}/radiomics_features_wide.csv"
WIDE_AUGMENTED_CSV="${OUTPUT_DIR}/radiomics_features_wide_augmented.csv"
WORK_DIR="${OUTPUT_DIR}/work"
INTERMEDIATE_DIR="${WORK_DIR}/intermediate"
LONG_CSV="${INTERMEDIATE_DIR}/radiomics_features_long.csv"
LONG_AUGMENTED_CSV="${INTERMEDIATE_DIR}/radiomics_features_long_augmented.csv"
ERRORS_CSV="${OUTPUT_DIR}/radiomics_features_errors.csv"

mkdir -p "${OUTPUT_DIR}" "${INTERMEDIATE_DIR}"

EXTRA_ARGS=()
case "${MODE}" in
  all)
    ;;
  original-only)
    EXTRA_ARGS+=(--disable-augmentation)
    rm -f "${LONG_AUGMENTED_CSV}" "${WIDE_AUGMENTED_CSV}"
    ;;
  augmentation-only)
    EXTRA_ARGS+=(--augmentation-only)
    ;;
  *)
    echo "Unknown mode: ${MODE}. Allowed: all, original-only, augmentation-only" >&2
    exit 2
    ;;
esac

"${PYTHON}" "${SCRIPT_DIR}/extract_radiomics_features.py" \
  "${MANIFEST_DIR}" \
  "${LONG_CSV}" \
  --config "${CONFIG_PATH}" \
  --work-dir "${WORK_DIR}" \
  --augmented-output-csv "${LONG_AUGMENTED_CSV}" \
  --errors-csv "${ERRORS_CSV}" \
  --jobs "${JOBS}" \
  "${EXTRA_ARGS[@]}"

if [[ "${MODE}" != "augmentation-only" ]]; then
  "${PYTHON}" "${SCRIPT_DIR}/concatenate_radiomics_features.py" \
    "${LONG_CSV}" \
    "${WIDE_CSV}" \
    --config "${CONFIG_PATH}"
fi

if [[ "${MODE}" != "original-only" && -f "${LONG_AUGMENTED_CSV}" ]]; then
  "${PYTHON}" "${SCRIPT_DIR}/concatenate_radiomics_features.py" \
    "${LONG_AUGMENTED_CSV}" \
    "${WIDE_AUGMENTED_CSV}" \
    --config "${CONFIG_PATH}"
else
  rm -f "${WIDE_AUGMENTED_CSV}"
fi

if [[ -f "${WIDE_CSV}" ]]; then
  echo "Wide features: ${WIDE_CSV}"
fi
if [[ -f "${WIDE_AUGMENTED_CSV}" ]]; then
  echo "Wide augmented features: ${WIDE_AUGMENTED_CSV}"
fi
if [[ -f "${ERRORS_CSV}" ]]; then
  echo "Errors: ${ERRORS_CSV}"
fi
