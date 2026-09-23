#!/usr/bin/env bash
# Static checks for loopback bind defaults, dotenv ignores, and dependency pins.
# Does not start the mock or read design-partner data.
set -euo pipefail
cd "$(dirname "$0")/../.."

fail() { echo "hardening check failed: $*" >&2; exit 1; }

grep -q 'CASHEW_BIND_HOST:-127.0.0.1' cashew/run.sh \
  || fail "run.sh must default the bind host to 127.0.0.1"
grep -q '127.0.0.1:8900:8900' cashew/docker-compose.yml \
  || fail "compose must publish 127.0.0.1:8900:8900"
grep -q -- '--host", "0.0.0.0"' cashew/Dockerfile \
  || fail "Dockerfile must keep the in-container bind on 0.0.0.0"
grep -q 'http://127.0.0.1:8900' cashew/engine/client.py \
  || fail "engine client default must be http://127.0.0.1:8900"

grep -q '^starlette==' cashew/requirements.txt \
  || fail "starlette must be pinned"
grep -q '^fastapi==' cashew/requirements.txt \
  || fail "fastapi must be pinned"
grep -q '^pytest==9\.' cashew/requirements.txt \
  || fail "pytest must be pinned to 9.x (CVE-2025-71176 is fixed in 9.0.3)"
if grep -E '^[A-Za-z0-9_.\[\]-]+>=' cashew/requirements.txt; then
  fail "requirements.txt must use exact pins"
fi

for f in .env .env.local .env.production cashew/.env.local cashew/.env.production .envrc cashew/.envrc; do
  git check-ignore -q -- "$f" || fail "$f should be ignored"
done
for f in cashew/.env.example hermes-finance/env.example; do
  if git check-ignore -q -- "$f"; then
    fail "$f should stay tracked"
  fi
done

echo "hardening checks ok"
