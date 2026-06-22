#!/bin/sh
# tory_notion_read.sh — headless 토리(claude)용 Notion 읽기 전용 헬퍼 (2026-06-11).
# headless claude 엔 Notion MCP 가 없다(그건 앱 전용·claude.ai 연결) → integration 토큰으로
# Notion REST 를 직접 읽는다. tory_slack_read.sh 와 같은 원칙: 읽기 메서드 화이트리스트,
# 토큰은 ~/.hermes/.env 에서 내부 주입만 하고 절대 출력하지 않으며, 쓰기/발송 메서드는 없다.
#
# 사용:
#   tory_notion_read.sh search '<질의>'       # 워크스페이스+연결소스 검색(최근 편집순)
#   tory_notion_read.sh page   '<page_id>'    # 페이지 속성
#   tory_notion_read.sh blocks '<block_id>'   # 페이지 본문 블록(자식)
#   tory_notion_read.sh db     '<database_id>'# 데이터베이스 쿼리(최근 10)
set -u
OP="${1:-}"; ARG="${2:-}"
ENV_FILE="${TORY_ENV_FILE:-}"
if [ -z "$ENV_FILE" ] && [ -n "${TORY_ASSISTANT_ID:-}" ] && [ -f "$HOME/.torymemory/assistants/$TORY_ASSISTANT_ID/.env" ]; then
  ENV_FILE="$HOME/.torymemory/assistants/$TORY_ASSISTANT_ID/.env"
fi
ENV_FILE="${ENV_FILE:-$HOME/.hermes/.env}"
TOK=$(/usr/bin/grep '^NOTION_TOKEN=' "$ENV_FILE" 2>/dev/null | /usr/bin/cut -d= -f2- | /usr/bin/tr -d '"' | /usr/bin/tr -d "'")
if [ -z "$TOK" ]; then
  printf '{"ok":false,"error":"no_token","hint":"https://www.notion.so/profile/integrations 에서 internal integration 발급 후 ~/.hermes/.env 의 NOTION_TOKEN 에 넣고, 그 integration 을 읽을 페이지/팀스페이스에 연결(Connections)하세요."}\n'
  exit 1
fi
AUTH="Authorization: Bearer $TOK"
VER="Notion-Version: 2022-06-28"
case "$OP" in
  search)
    BODY=$(printf '%s' "$ARG" | /usr/bin/python3 -c 'import json,sys;print(json.dumps({"query":sys.stdin.read().strip(),"page_size":10,"sort":{"direction":"descending","timestamp":"last_edited_time"}}))')
    exec /usr/bin/curl -s --max-time 20 -X POST "https://api.notion.com/v1/search" \
      -H "$AUTH" -H "$VER" -H "Content-Type: application/json" --data "$BODY" ;;
  page)
    exec /usr/bin/curl -s --max-time 15 -H "$AUTH" -H "$VER" "https://api.notion.com/v1/pages/$ARG" ;;
  blocks)
    exec /usr/bin/curl -s --max-time 15 -H "$AUTH" -H "$VER" "https://api.notion.com/v1/blocks/$ARG/children?page_size=50" ;;
  db)
    exec /usr/bin/curl -s --max-time 20 -X POST "https://api.notion.com/v1/databases/$ARG/query" \
      -H "$AUTH" -H "$VER" -H "Content-Type: application/json" --data '{"page_size":10}' ;;
  *)
    printf '{"ok":false,"error":"op_not_allowed: search|page|blocks|db (읽기 전용 헬퍼)"}\n'; exit 1 ;;
esac
