#!/usr/bin/env python3
"""
tory_drive_read.py — headless 토리용 Google Drive 읽기 전용 헬퍼 (2026-06-12).

headless 워처/조사 에이전트엔 Drive MCP 가 없다 → google_token.json(회사 Google .readonly OAuth,
google_fetch 가 쓰는 토큰)으로 Drive REST 를 직접 읽는다. tory_gmail_read.py 와 같은 원칙:
읽기 op 화이트리스트, 토큰 내부 주입(절대 출력 안 함), 발송/수정/삭제 없음, 본문 redact.

사용:
  tory_drive_read.py search '<keyword>'   # 제목·본문 fullText 검색 → 파일 목록(제목·소유자·수정일)
  tory_drive_read.py read '<file_id>'      # 구글 문서면 본문 텍스트, 그 외엔 메타만
"""
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
DAPI = "https://www.googleapis.com/drive/v3"
OAUTH = "https://oauth2.googleapis.com/token"

sys.path.insert(0, os.path.join(HOME, ".torymemory", "bin"))
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
    req = urllib.request.Request(DAPI + path, headers={"Authorization": "Bearer " + at})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


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
            params = urllib.parse.urlencode({
                "q": "fullText contains '%s'" % arg.replace("'", "\\'"),
                "pageSize": 15, "orderBy": "modifiedTime desc",
                "fields": "files(id,name,mimeType,modifiedTime,owners(emailAddress))",
            })
            d = _get("/files?" + params, at)
            out = [{"id": f["id"], "name": redact(f.get("name", "")),
                    "type": (f.get("mimeType", "").split(".")[-1]),
                    "modified": f.get("modifiedTime", ""),
                    "owner": (f.get("owners") or [{}])[0].get("emailAddress", "")}
                   for f in d.get("files", [])]
            print(json.dumps({"ok": True, "files": out}, ensure_ascii=False))
        elif op == "read":
            meta = _get("/files/%s?fields=id,name,mimeType" % arg, at)
            mt = meta.get("mimeType", "")
            if "google-apps" in mt:
                url = DAPI + "/files/%s/export?mimeType=%s" % (arg, urllib.parse.quote("text/plain"))
                req = urllib.request.Request(url, headers={"Authorization": "Bearer " + at})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", "replace")
                print(json.dumps({"ok": True, "name": redact(meta.get("name", "")),
                                  "text": redact(body)[:4000]}, ensure_ascii=False))
            else:
                print(json.dumps({"ok": True, "name": redact(meta.get("name", "")),
                                  "note": "비구글문서 — 메타만", "type": mt}, ensure_ascii=False))
        else:
            print(json.dumps({"ok": False, "error": "op: search|read (읽기 전용)"}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:120]}))


if __name__ == "__main__":
    main()
