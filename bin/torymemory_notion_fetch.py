#!/usr/bin/env python3
"""
torymemory_notion_fetch.py — 회사 Notion 문서를 헤르메스 feed 로 적재하는 읽기전용 폴러 (2026-06-11).

headless 헤르메스(큐레이터 컨테이너)엔 Notion MCP 가 없다 → NOTION_TOKEN(~/.hermes/.env, internal
integration)으로 Notion REST 를 폴링해, 최근 편집 문서의 제목·본문 발췌를 redact 후
~/.torymemory/feeds/notion/ 에 JSONL(kind=notion_doc, scope=company)로 떨군다. harvest 가 그걸
줍어 awm_confidential 기억으로 큐레이션한다 → "헤르메스가 노션을 본다".

slack_fetch/google_fetch 와 동형. 발송·쓰기 없음(읽기 전용). 토큰 없으면 깨끗이 종료(launchd 에러 방지).
"""
import datetime
import json
import os
import socket
import sys
import urllib.request

socket.setdefaulttimeout(30)

HOME = os.path.expanduser("~")
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

ENV_FILE = PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env")
FEED_DIR = (PROFILE.get("feed_dirs") or {}).get("notion") or os.path.join(HOME, ".torymemory", "feeds", "notion")
STATE_DIR = PROFILE.get("state_dir") or os.path.join(HOME, ".torymemory", "state")
CURSOR = os.path.join(STATE_DIR, "notion-cursor.json")
API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_DOCS = int(os.environ.get("NOTION_MAX_DOCS", "20"))
EXCERPT_CAP = int(os.environ.get("NOTION_EXCERPT_CAP", "700"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
try:
    from torymemory_redact_secrets import redact
except Exception:
    def redact(t):
        return t


def _token():
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("NOTION_TOKEN="):
                    return line[len("NOTION_TOKEN="):].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _api(method, path, body=None, tok=""):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + tok,
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def _title_of(page):
    props = page.get("properties") or {}
    for v in props.values():
        if isinstance(v, dict) and v.get("type") == "title" and v.get("title"):
            return "".join(x.get("plain_text", "") for x in v["title"]).strip() or "(제목없음)"
    return "(제목없음)"


def _plain(blocks):
    out = []
    for b in (blocks.get("results") or []):
        t = b.get("type")
        node = b.get(t) if isinstance(b.get(t), dict) else None
        rich = node.get("rich_text") if node else None
        if rich:
            out.append("".join(x.get("plain_text", "") for x in rich))
    return " ".join(s for s in out if s).strip()


def main():
    tok = _token()
    if not tok:
        print(json.dumps({"ok": False, "skip": "no_token",
                          "hint": "~/.hermes/.env 의 NOTION_TOKEN 발급·연결 후 활성화"}))
        return
    os.makedirs(FEED_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        cur = json.load(open(CURSOR)).get("last_edited", "")
    except Exception:
        cur = ""
    try:
        res = _api("POST", "/search",
                   {"query": "", "page_size": MAX_DOCS,
                    "sort": {"direction": "descending", "timestamp": "last_edited_time"}},
                   tok)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:140]}))
        return
    recs, newest = [], cur
    for it in (res.get("results") or []):
        le = it.get("last_edited_time", "")
        if cur and le and le <= cur:
            continue
        if le > newest:
            newest = le
        pid = it.get("id")
        title = _title_of(it)
        excerpt = ""
        try:
            excerpt = _plain(_api("GET", "/blocks/%s/children?page_size=25" % pid, None, tok))[:EXCERPT_CAP]
        except Exception:
            pass
        text = "[노션 문서] %s — %s" % (title, excerpt) if excerpt else "[노션 문서] %s" % title
        recs.append({"v": 1, "kind": "notion_doc", "host": "notion", "scope": "company",
                     "channel_id": pid, "channel": title, "user": "notion",
                     "ts": "", "iso": le, "text": redact(text)})
    if recs:
        path = os.path.join(FEED_DIR, "notion-%s.jsonl" % datetime.datetime.now().strftime("%Y%m%d"))
        with open(path, "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
    try:
        json.dump({"last_edited": newest}, open(CURSOR, "w"))
    except OSError:
        pass
    print(json.dumps({"ok": True, "new_docs": len(recs)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
