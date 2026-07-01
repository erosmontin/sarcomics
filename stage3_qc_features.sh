#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: ./stage3_qc_features.sh [features_dir] [manifest_dir] [config_path]"
  echo
  echo "Checks wide-table patient rows, modality feature counts, and writes all wide features for review."
  echo
  echo "Default features_dir: radiomics_features"
  echo "Default manifest_dir: radiomics_manifests"
  echo "Default config_path: radiomics_config.yaml next to this script"
  echo "Default output_dir: <features_dir>/qc_features"
  exit 0
fi

FEATURES_DIR="${1:-radiomics_features}"
MANIFEST_DIR="${2:-radiomics_manifests}"
CONFIG_PATH="${3:-${SCRIPT_DIR}/radiomics_config.yaml}"

"${PYTHON}" "${SCRIPT_DIR}/stage3_qc_features.py" \
  "${FEATURES_DIR}" \
  "${MANIFEST_DIR}" \
  "${CONFIG_PATH}"
