#!/usr/bin/env bash
# Start the Cashew mock harness locally (venv). Pass extra uvicorn flags if needed.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn mocks.app:app --host 0.0.0.0 --port 8900 "$@"
