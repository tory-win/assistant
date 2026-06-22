#!/bin/sh
# torymemory_sync_macbook.sh — pull the MacBook's session logs to the Mac mini
# staging so the SINGLE Hermes curator (Mac mini) covers BOTH machines.
#
# Runs on the Mac mini HOST (not in the container — the container can't reach the
# MacBook over Tailscale). Read-only rsync over SSH. Fail-safe: if the MacBook is
# offline / the key isn't authorized yet, it logs and exits 0.
#
# Env:
#   MACBOOK_SSH      tory@<tailscale-ip-or-host>   (default tory@100.99.147.82 = node)
#   MACBOOK_SSH_KEY  ~/.ssh/torymemory_sync
#   MACBOOK_STAGE    ~/.torymemory/feeds/macbook
set -u
MB="${MACBOOK_SSH:-aswemake@100.99.147.82}"
KEY="${MACBOOK_SSH_KEY:-$HOME/.ssh/torymemory_sync}"
STAGE="${MACBOOK_STAGE:-$HOME/.torymemory/feeds/macbook}"
LOG="${MACBOOK_SYNC_LOG:-$HOME/.torymemory/macbook-sync.log}"
SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
mkdir -p "$STAGE/claude" "$STAGE/codex" "$(dirname "$LOG")" 2>/dev/null
ts() { date '+%Y-%m-%dT%H:%M:%S'; }

if ! $SSH "$MB" 'true' >/dev/null 2>&1; then
  echo "[$(ts)] macbook 도달 불가 ($MB) — 스킵 (오프라인이거나 키 미등록)" >> "$LOG"
  exit 0
fi
# 읽기전용 pull. --delete 안 씀(로그는 늘기만 함; 스테이징에서 임의 삭제 방지).
rsync -az --timeout=60 -e "$SSH" "$MB:.claude/projects/" "$STAGE/claude/" >> "$LOG" 2>&1 && c=ok || c=fail
rsync -az --timeout=60 -e "$SSH" "$MB:.codex/sessions/" "$STAGE/codex/" >> "$LOG" 2>&1 && x=ok || x=fail
echo "[$(ts)] 동기화 claude=$c codex=$x" >> "$LOG"
