#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LIVE="${LIVE_ASSISTANT_HOME:-$HOME/.torymemory}"
TORYMEMORY_REPO="${TORYMEMORY_REPO:-$HOME/Downloads/dev/torymemory}"

rsync -a --delete --exclude='*.bak*' --exclude='__pycache__/' "$LIVE/bin/" "$ROOT/bin/"
rsync -a --delete --exclude='*.bak*' "$TORYMEMORY_REPO/docker/tory-agent/" "$ROOT/docker/tory-agent/"
cp "$TORYMEMORY_REPO/지침/personal/private/torymemory_setup/11_토리_비서.md" "$ROOT/docs/토리_비서.md"

echo "synced bin/docker/docs from live sources; compose files stay assistant-owned"
