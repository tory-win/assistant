#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LIVE="${LIVE_ASSISTANT_HOME:-$HOME/.torymemory}"

rsync -a --delete --exclude='*.bak*' --exclude='__pycache__/' "$LIVE/bin/" "$ROOT/bin/"

echo "synced live shim bin/ into assistant/bin; docker/docs stay assistant-owned"
