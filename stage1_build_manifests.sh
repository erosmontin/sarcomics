#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${SCRIPT_DIR}/runner.py"
SUMMARIZER="${SCRIPT_DIR}/summarize_manifests.py"
IMAGES_ROOT="${1:-1_3D_images}"
OUTPUT_DIR="${2:-radiomics_manifests}"
CONFIG_PATH="${3:-${SCRIPT_DIR}/radiomics_config.yaml}"
PYTHON="${PYTHON:-python3}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: ./stage1_build_manifests.sh [images_root] [output_dir] [config_path]"
  echo
  echo "Default images_root: 1_3D_images"
  echo "Default output_dir: radiomics_manifests"
  echo "Default config_path: radiomics_config.yaml next to this script"
  exit 0
fi

if [[ ! -d "${IMAGES_ROOT}" ]]; then
  echo "Images root not found: ${IMAGES_ROOT}" >&2
  exit 2
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config file not found: ${CONFIG_PATH}" >&2
  exit 2
fi

mkdir -p "${OUTPUT_DIR}"

total=0
failed=0

while IFS= read -r -d '' patient_dir; do
  patient_id="$(basename "${patient_dir}")"
  output_json="${OUTPUT_DIR}/${patient_id}.json"
  total=$((total + 1))

  if ! "${PYTHON}" "${RUNNER}" "${patient_dir}" "${output_json}" --config "${CONFIG_PATH}"; then
    failed=$((failed + 1))
    echo "${patient_id}: failed" >&2
  fi
done < <(find "${IMAGES_ROOT}" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

echo "Processed ${total} patient directories. JSON manifests: ${OUTPUT_DIR}"

if [[ "${failed}" -gt 0 ]]; then
  echo "${failed} patient directories failed." >&2
  exit 1
fi

"${PYTHON}" "${SUMMARIZER}" "${OUTPUT_DIR}" "${OUTPUT_DIR}/summary.json" --print-table
