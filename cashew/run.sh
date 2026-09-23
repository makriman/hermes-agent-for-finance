#!/usr/bin/env bash
# Start the Cashew mock harness locally (venv). Pass extra uvicorn flags if needed.
# The API has no auth. Bind loopback unless CASHEW_BIND_HOST is set.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn mocks.app:app --host "${CASHEW_BIND_HOST:-127.0.0.1}" --port 8900 "$@"
