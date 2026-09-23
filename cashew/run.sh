#!/usr/bin/env bash
# Start the Cashew mock harness locally (venv). Pass extra uvicorn flags if needed.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
# No auth on this harness. Bind the loopback interface only.
exec uvicorn mocks.app:app --host 127.0.0.1 --port 8900 "$@"
