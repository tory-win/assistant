#!/bin/sh
# tory_slack_read.sh — headless 토리(claude)용 Slack 읽기 전용 헬퍼.
# 사용: tory_slack_read.sh <method> '<url-encoded querystring>'
# 예  : tory_slack_read.sh conversations.history 'channel=C0A71A0971D&limit=30'
#       tory_slack_read.sh search.messages 'query=IR코리아&count=10'
#
# 화이트리스트 밖 메서드는 거부 — chat.postMessage 등 쓰기/발송은 구조적으로 불가.
# 토큰은 ~/.hermes/.env 에서 내부 주입만 하고 절대 출력하지 않는다.
set -u
M="${1:-}"
P="${2:-}"
case "$M" in
  conversations.history|conversations.replies|conversations.info|conversations.list|users.info|users.list|search.messages) ;;
  *) printf '{"ok":false,"error":"method_not_allowed_readonly_helper"}\n'; exit 1 ;;
esac
ENV_FILE="${TORY_ENV_FILE:-}"
if [ -z "$ENV_FILE" ] && [ -n "${TORY_ASSISTANT_ID:-}" ] && [ -f "$HOME/.torymemory/assistants/$TORY_ASSISTANT_ID/.env" ]; then
  ENV_FILE="$HOME/.torymemory/assistants/$TORY_ASSISTANT_ID/.env"
fi
ENV_FILE="${ENV_FILE:-$HOME/.hermes/.env}"
TOK=$(/usr/bin/grep '^SLACK_USER_TOKEN=' "$ENV_FILE" 2>/dev/null | /usr/bin/cut -d= -f2- | /usr/bin/tr -d '"' | /usr/bin/tr -d "'")
if [ -z "$TOK" ]; then
  printf '{"ok":false,"error":"no_token"}\n'; exit 1
fi
exec /usr/bin/curl -s --max-time 15 -H "Authorization: Bearer $TOK" "https://slack.com/api/$M?$P"
