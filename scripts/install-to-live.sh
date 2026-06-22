#!/bin/sh
set -eu

if [ "${CONFIRM:-}" != "install" ]; then
  echo "Set CONFIRM=install to copy bin/ into ~/.torymemory/bin" >&2
  exit 2
fi

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LIVE="${LIVE_ASSISTANT_HOME:-$HOME/.torymemory}"

rsync -a --delete --exclude='__pycache__/' "$ROOT/bin/" "$LIVE/bin/"
chmod +x "$LIVE"/bin/*.py "$LIVE"/bin/*.sh 2>/dev/null || true

echo "installed bin/ to $LIVE/bin"

