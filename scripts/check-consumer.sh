#!/usr/bin/env bash
# Builds the sdist and wheel, installs the wheel into a fresh virtual environment
# with FastAPI, uvicorn and Flask, and runs both frameworks against a stand-in
# collector. Proves the published artifact works outside this checkout.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/agentrail-python-consumer.XXXXXXXX")"
trap 'rm -rf "$scratch"' EXIT
python="${PYTHON:-3.12}"
cd "$root"
uv build --out-dir "$scratch/dist" >/dev/null
wheel="$(ls "$scratch"/dist/*.whl)"
uv venv --quiet --python "$python" "$scratch/venv"
# Only releases at least two weeks old, as the lockfile.
uv pip install --quiet --python "$scratch/venv/bin/python" --exclude-newer 2026-10-28T00:00:00Z \
  "$wheel" "fastapi>=0.115" "uvicorn>=0.38" "flask>=3.1"
cd "$scratch"
"$scratch/venv/bin/python" -I "$root/scripts/consumer_apps.py"
