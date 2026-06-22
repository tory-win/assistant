#!/bin/sh
# 토리 6잡 주기 실행. 헬퍼/잡은 assistant/bin이 마운트된 /root/.torymemory/bin,
# 토큰은 ~/.hermes, 상태·feed 는 ~/.torymemory/{state,feeds}(rw 마운트). claude -p 는 host :8321.
set -u
BIN=/root/.torymemory/bin
export HOME=/root
# compose 에서는 :8321 을 local-cliproxyapi 로 직접 라우팅한다. 기본값은 수동 실행 fallback.
HOSTBASE="${TORY_HOST:-host.docker.internal}"
# 조사 엔진(watcher/replier)은 tory_agent_llm 의 HTTP 도구루프 → :8321(claude -p 바이너리 제거).
# 메모리(:1128)·브리프 LLM(:8321)도 host 로 라우팅. 스크립트는 OS env 우선이라 .env 의 localhost 를 덮어쓴다.
export TORY_LLM_BASE="${TORY_LLM_BASE:-http://$HOSTBASE:8321}"
export TORYMEMORY_API="${TORYMEMORY_API:-http://$HOSTBASE:1128}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://$HOSTBASE:8321/v1}"
export TORY_MEETING_NOTES_FILE="${TORY_MEETING_NOTES_FILE:-/root/.torymemory/meeting-note-studio-data/notes.json}"
export PATH="/usr/local/bin:/usr/bin:/bin"
WORK_STATE="$(PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import os
import sys
sys.path.insert(0, "/root/.torymemory/bin")
try:
    import tory_assistant_config as c
    p = c.load_profile()
    print(os.path.join(p.get("state_dir") or "/root/.torymemory/state", "work-hours.json"))
except Exception:
    print("/root/.torymemory/state/work-hours.json")
PY
)"

log() { echo "[$(date '+%F %T')] tory-agent: $*"; }
is_offwork() {
  python3 - "$WORK_STATE" <<'PY'
import json
import sys
import time

try:
    state = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
active = bool(state.get("offwork")) and time.time() < float(state.get("resume_at") or 0)
sys.exit(0 if active else 1)
PY
}
log "start (LLM=$TORY_LLM_BASE memory=$TORYMEMORY_API)"

lf=0; lb=0; lr=0; lh=0
while true; do
  now=$(date +%s)
  # fetch (slack/google/notion) — 5분
  if [ $((now - lf)) -ge 300 ]; then
    python3 "$BIN/torymemory_slack_fetch.py"  >/dev/null 2>&1 || log "slack_fetch err"
    python3 "$BIN/torymemory_google_fetch.py" >/dev/null 2>&1 || log "google_fetch err"
    python3 "$BIN/torymemory_notion_fetch.py" >/dev/null 2>&1 || log "notion_fetch err"
    python3 "$BIN/tory_recording_fetch.py" >/dev/null 2>&1 || log "recording_fetch err"
    lf=$now
  fi
  # brief — 2분
  if [ $((now - lb)) -ge 120 ]; then
    if is_offwork; then
      :
    else
      python3 "$BIN/torymemory_slack_brief.py" >/dev/null 2>&1 || log "brief err"
      lb=$now
    fi
  fi
  # 햇배달 사업 요약 — 별도 메시지, 1시간마다 체크(스크립트 내부에서 변경/6시간 가드)
  if [ $((now - lh)) -ge 3600 ]; then
    if is_offwork; then
      :
    else
      python3 "$BIN/tory_hatdelivery_digest.py" >/dev/null 2>&1 || log "hatdelivery_digest err"
      lh=$now
    fi
  fi
  # watcher — 매 루프(7초): 비서 채널 명령 응답(claude -p)
  python3 "$BIN/tory_command_watcher.py" --once >/dev/null 2>&1 || true
  # replier — 15분: 게이트(조사 제안/승인 조사)
  if [ $((now - lr)) -ge 900 ]; then
    if is_offwork; then
      :
    else
      python3 "$BIN/tory_research_replier.py" >/dev/null 2>&1 || log "replier err"
      lr=$now
    fi
  fi
  sleep 7
done
