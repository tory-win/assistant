#!/usr/bin/env python3
"""
tory_gmail_read.py — headless 토리용 Gmail 읽기 전용 헬퍼 (2026-06-11).

headless 워처/큐레이터엔 Gmail MCP 가 없다 → google_token.json(회사 Gmail .readonly OAuth,
google_fetch 가 쓰는 토큰)으로 Gmail REST 를 직접 읽는다. tori/tory_slack_read.sh 와 같은 원칙:
읽기 op 화이트리스트, 토큰 내부 주입(절대 출력 안 함), 발송/수정/라벨변경 없음, 본문 redact.

사용:
  tory_gmail_read.py search '<gmail query>'   # 스레드 목록(스니펫)  예: 'from:snowflake newer_than:30d'
  tory_gmail_read.py thread '<thread_id>'      # 스레드 전체(발신·제목·본문)
  tory_gmail_read.py message '<message_id>'    # 단일 메시지 본문
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

TOKEN_DIR = PROFILE.get("google_token_dir") or os.path.dirname(PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env"))
TOKEN = PROFILE.get("google_token_file") or os.path.join(os.path.expanduser(TOKEN_DIR), "google_token.json")
GAPI = "https://gmail.googleapis.com/gmail/v1/users/me"
OAUTH = "https://oauth2.googleapis.com/token"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
try:
    from torymemory_redact_secrets import redact
except Exception:
    def redact(t):
        return t


def _access_token():
    with open(TOKEN, encoding="utf-8") as f:
        t = json.load(f)
    data = urllib.parse.urlencode({
        "client_id": t["client_id"], "client_secret": t["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(OAUTH, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]


def _get(path, at):
    req = urllib.request.Request(GAPI + path, headers={"Authorization": "Bearer " + at})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _decode(data):
    try:
        return base64.urlsafe_b64decode((data or "").encode()).decode("utf-8", "replace")
    except Exception:
        return ""


def _body(payload):
    if (payload.get("mimeType") or "").startswith("text/plain"):
        return _decode((payload.get("body") or {}).get("data", ""))
    for p in payload.get("parts", []) or []:
        b = _body(p)
        if b:
            return b
    return ""


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if not os.path.exists(TOKEN):
        print(json.dumps({"ok": False, "error": "no_google_token"})); return
    try:
        at = _access_token()
    except Exception as e:
        print(json.dumps({"ok": False, "error": "auth:" + str(e)[:90]})); return
    try:
        if op == "search":
            d = _get("/threads?maxResults=10&q=" + urllib.parse.quote(arg), at)
            print(json.dumps({"ok": True, "threads": [
                {"id": t["id"], "snippet": redact(t.get("snippet", ""))[:200]}
                for t in d.get("threads", [])]}, ensure_ascii=False))
        elif op == "thread":
            d = _get("/threads/" + arg + "?format=full", at)
            out = []
            for m in d.get("messages", []):
                h = {x["name"]: x["value"] for x in (m.get("payload", {}).get("headers", []) or [])}
                out.append({"from": h.get("From", ""), "subject": redact(h.get("Subject", "")),
                            "date": h.get("Date", ""), "body": redact(_body(m.get("payload", {})))[:2000]})
            print(json.dumps({"ok": True, "messages": out}, ensure_ascii=False))
        elif op == "message":
            m = _get("/messages/" + arg + "?format=full", at)
            h = {x["name"]: x["value"] for x in (m.get("payload", {}).get("headers", []) or [])}
            print(json.dumps({"ok": True, "from": h.get("From", ""), "subject": redact(h.get("Subject", "")),
                              "body": redact(_body(m.get("payload", {})))[:3000]}, ensure_ascii=False))
        else:
            print(json.dumps({"ok": False, "error": "op: search|thread|message (읽기 전용)"}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:120]}))


if __name__ == "__main__":
    main()
