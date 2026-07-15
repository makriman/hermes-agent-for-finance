#!/usr/bin/env bash
# Apply the hermes-finance layer onto a hermes-agent checkout.
#
#   ./apply.sh /path/to/hermes-agent
#
# Copies the native WhatsApp Cloud adapter into gateway/platforms/ and applies
# the five upstream patches. Idempotent-ish: re-running re-copies the adapter and
# will report already-applied patches rather than double-applying.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-}"

if [[ -z "$TARGET" || ! -d "$TARGET/gateway" ]]; then
  echo "usage: $0 /path/to/hermes-agent  (must contain gateway/)" >&2
  exit 1
fi

BASE="$(cat "$HERE/UPSTREAM_BASE_COMMIT.txt" 2>/dev/null || true)"
CUR="$(git -C "$TARGET" rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ -n "$BASE" && "$CUR" != "$BASE" ]]; then
  echo "⚠  hermes-agent is at $CUR but patches were built for $BASE."
  echo "   Patches may not apply cleanly; apply hunks by hand if needed."
fi

echo "→ copying platforms/whatsapp_cloud.py"
cp "$HERE/platforms/whatsapp_cloud.py" "$TARGET/gateway/platforms/whatsapp_cloud.py"

declare -A MAP=(
  [agent__prompt_builder.py.patch]="agent/prompt_builder.py"
  [gateway__config.py.patch]="gateway/config.py"
  [gateway__run.py.patch]="gateway/run.py"
  [tools__send_message_tool.py.patch]="tools/send_message_tool.py"
  [tools__tts_tool.py.patch]="tools/tts_tool.py"
)

for patch in "${!MAP[@]}"; do
  f="${MAP[$patch]}"
  echo "→ applying $patch → $f"
  if git -C "$TARGET" apply --check "$HERE/patches/$patch" 2>/dev/null; then
    git -C "$TARGET" apply "$HERE/patches/$patch"
  elif git -C "$TARGET" apply --reverse --check "$HERE/patches/$patch" 2>/dev/null; then
    echo "   (already applied — skipping)"
  else
    echo "   ✗ does not apply cleanly — apply $patch by hand" >&2
  fi
done

echo "✓ done. Now merge config.yaml.example into ~/.hermes/config.yaml,"
echo "  fill ~/.hermes/.env from env.example, and copy skills/cashew into ~/.hermes/skills/."
