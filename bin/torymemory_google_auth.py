#!/usr/bin/env python3
"""
torymemory_google_auth.py — 토리 비서 Google 읽기전용 OAuth (Desktop, loopback 47823).

3단계: --auth-url(동의 URL+verifier 저장) → --catch(로컬 서버가 리다이렉트의 code 를 파일로 기록)
→ --exchange(code→토큰 저장). 브라우저는 에이전트가 몰아 Allow 클릭. code 는 파일로 받으므로
stdout 버퍼 문제 없음. 읽기전용 scope만(gmail/calendar/drive .readonly).
hermes venv python 으로 실행.
"""
import argparse
import http.server
import json
import os
import urllib.parse

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
from google_auth_oauthlib.flow import Flow  # noqa: E402

CLIENT_SECRET = os.path.expanduser("~/.torymemory/google_client_secret.json")
PORT = 47823
REDIRECT_URI = "http://localhost:%d" % PORT
SCOPES = [
    # 읽기전용(브리핑 페처가 의존 — 절대 빼지 말 것)
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    # 쓰기(2026-06-15, 승인 게이트 하 실행 — 캘린더 일정·Gmail 초안/발송)
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.compose",
]
STATE_DIR = os.path.expanduser("~/.torymemory/state")
PENDING = os.path.join(STATE_DIR, "google-oauth-pending.json")
CODE_FILE = os.path.join(STATE_DIR, "google-oauth-code.txt")


def token_path(account):
    suffix = "" if account == "company" else "_" + account
    return os.path.expanduser("~/.hermes/google_token%s.json" % suffix)


def cmd_auth_url():
    os.makedirs(STATE_DIR, exist_ok=True)
    flow = Flow.from_client_secrets_file(CLIENT_SECRET, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="false")
    with open(PENDING, "w") as f:
        json.dump({"state": state, "code_verifier": getattr(flow, "code_verifier", None)}, f)
    os.chmod(PENDING, 0o600)
    print(url)


def cmd_catch():
    try:
        os.remove(CODE_FILE)
    except OSError:
        pass
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (params.get("code") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("토리 인증 완료. 이 창은 닫아도 됩니다.".encode("utf-8"))
            if code:
                with open(CODE_FILE, "w") as f:
                    f.write(code)
                os.chmod(CODE_FILE, 0o600)
                got["code"] = code

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("localhost", PORT), H)
    while "code" not in got:
        srv.handle_request()
    print("CODE_CAPTURED")


def cmd_exchange(account):
    with open(CODE_FILE) as f:
        code = f.read().strip()
    pj = json.load(open(PENDING)) if os.path.exists(PENDING) else {}
    flow = Flow.from_client_secrets_file(CLIENT_SECRET, scopes=SCOPES, redirect_uri=REDIRECT_URI,
                                         state=pj.get("state"))
    if pj.get("code_verifier"):
        flow.code_verifier = pj["code_verifier"]
    flow.fetch_token(code=code)
    c = flow.credentials
    data = {
        "token": c.token, "refresh_token": c.refresh_token, "token_uri": c.token_uri,
        "client_id": c.client_id, "client_secret": c.client_secret,
        "scopes": list(c.scopes or SCOPES),
        "expiry": c.expiry.isoformat() if getattr(c, "expiry", None) else None,
        "account": account,
    }
    p = token_path(account)
    with open(p, "w") as f:
        json.dump(data, f)
    os.chmod(p, 0o600)
    print("SAVED: %s | refresh_token=%s | scopes=%d" % (p, bool(c.refresh_token), len(data["scopes"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-url", action="store_true")
    ap.add_argument("--catch", action="store_true")
    ap.add_argument("--exchange", action="store_true")
    ap.add_argument("--account", default="company", choices=["company", "personal"])
    args = ap.parse_args()
    if args.auth_url:
        cmd_auth_url()
    elif args.catch:
        cmd_catch()
    elif args.exchange:
        cmd_exchange(args.account)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
