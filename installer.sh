#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$SCRIPT_DIR/installer.py" "$@"
fi

if command -v python >/dev/null 2>&1; then
    exec python "$SCRIPT_DIR/installer.py" "$@"
fi

if command -v conda >/dev/null 2>&1; then
    CONDA_BIN_DIR=$(dirname "$(command -v conda)")
    if [ -x "$CONDA_BIN_DIR/python" ]; then
        exec "$CONDA_BIN_DIR/python" "$SCRIPT_DIR/installer.py" "$@"
    fi
fi

for candidate in \
    "$HOME/miniconda3/bin/python" \
    "$HOME/anaconda3/bin/python" \
    "/opt/conda/bin/python" \
    "/opt/miniconda3/bin/python" \
    "/opt/anaconda3/bin/python"
do
    if [ -x "$candidate" ]; then
        exec "$candidate" "$SCRIPT_DIR/installer.py" "$@"
    fi
done

echo "ERROR: Python was not found. Install Miniconda/Anaconda, then rerun this installer." >&2
exit 1
