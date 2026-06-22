#!/bin/sh
# tory_slack_send.sh — 토리 발신 큐 입력기. **직접 발송하지 않는다.**
# 메시지를 ~/.torymemory/outbox/ 에 적재하면, 헤르메스 워처
# (launchd com.tory.tory-command-watcher)가 수 초 내 토리 봇 명의로
# 비서 채널(C0B997W7KGS)에 발송한다. 발신 단일 관문 = 헤르메스 워처.
# 이 스크립트는 토큰을 읽지 않으며 네트워크 호출도 하지 않는다.
#
# 사용: tory_slack_send.sh [--thread <ts>] <<'EOF'
#       본문...
#       EOF
set -u
TS=""
if [ "${1:-}" = "--thread" ]; then
  TS="${2:-}"
fi
TEXT=$(/bin/cat)
if [ -z "$TEXT" ]; then
  printf '{"ok":false,"error":"empty_text"}\n'; exit 1
fi
OUTDIR="$HOME/.torymemory/outbox"
/bin/mkdir -p "$OUTDIR"
export _TORI_TEXT="$TEXT" _TORI_TS="$TS" _TORI_OUT="$OUTDIR"
exec /usr/bin/python3 -c '
import json, os, time
d = os.environ["_TORI_OUT"]
name = "msg-%d-%05d.json" % (time.time() * 1000, os.getpid() % 100000)
tmp = os.path.join(d, "." + name + ".tmp")
obj = {"text": os.environ["_TORI_TEXT"][:11500],
       "thread_ts": os.environ["_TORI_TS"] or None,
       "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
with open(tmp, "w") as f:
    json.dump(obj, f, ensure_ascii=False)
os.replace(tmp, os.path.join(d, name))
print(json.dumps({"ok": True, "queued": name, "via": "hermes-watcher"}))
'
