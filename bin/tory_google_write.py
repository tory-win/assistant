#!/usr/bin/env python3
"""
tory_google_write.py — 승인 게이트 하 Google 쓰기 (2026-06-15).

캘린더 일정 등록 / Gmail 초안 생성. 컨테이너에서 ~/.hermes/google_token.json(쓰기 스코프 포함)을
google-auth 로 refresh 해 호출한다. **직접 실행은 send_gate 가 보스 ✅ 후에만** 부른다 — 이 모듈
자체는 게이트가 아니다(호출 = 실행). 읽기전용 스코프는 그대로라 브리핑 페처와 공존한다.

스코프: calendar.events(일정 쓰기), gmail.compose(초안). Gmail 은 *초안만* 만든다(발송은 보스가
Gmail 에서 직접 — 타인 메일 자동발송 회피). stdlib + google-auth 만(google-api-python-client 불필요).
"""
import base64
import json
import os
import urllib.error
import urllib.request
from email.message import EmailMessage

try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

HOME = os.path.expanduser("~")
TOKEN_DIR = PROFILE.get("google_token_dir") or os.path.dirname(PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env"))
TOKEN = os.path.expanduser(PROFILE.get("google_token_file") or os.path.join(TOKEN_DIR, "google_token.json"))


def _creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    t = json.load(open(TOKEN))
    c = Credentials(token=t.get("token"), refresh_token=t.get("refresh_token"),
                    token_uri=t.get("token_uri"), client_id=t.get("client_id"),
                    client_secret=t.get("client_secret"), scopes=t.get("scopes"))
    c.refresh(Request())
    return c


def _api(c, method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + c.token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def calendar_event(summary, start, end, description="", attendees=None):
    """일정 등록. start/end = 'YYYY-MM-DD'(종일) 또는 RFC3339('2026-06-20T14:00:00'). (ok, link|err)."""
    try:
        c = _creds()
    except Exception as e:
        return False, "Google 인증 실패: %s" % str(e)[:120]
    ev = {"summary": summary, "description": description or ""}
    ev["start"] = {"date": start} if len(start) == 10 else {"dateTime": start, "timeZone": "Asia/Seoul"}
    ev["end"] = {"date": end} if len(end) == 10 else {"dateTime": end, "timeZone": "Asia/Seoul"}
    if attendees:
        ev["attendees"] = [{"email": a} for a in attendees]
    s, r = _api(c, "POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", ev)
    if s == 200:
        return True, r.get("htmlLink") or r.get("id") or "(등록됨)"
    return False, "캘린더 등록 실패(%s): %s" % (s, (r.get("error") or {}).get("message", ""))


def gmail_draft(to, subject, body):
    """Gmail 초안 생성(발송 아님 — 보스가 Gmail 에서 확인/발송). (ok, draft_id|err)."""
    try:
        c = _creds()
    except Exception as e:
        return False, "Google 인증 실패: %s" % str(e)[:120]
    m = EmailMessage()
    m["To"] = to
    m["Subject"] = subject
    m.set_content(body or "")
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
    s, r = _api(c, "POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts", {"message": {"raw": raw}})
    if s == 200:
        return True, r.get("id") or "(초안 생성됨)"
    return False, "Gmail 초안 실패(%s): %s" % (s, (r.get("error") or {}).get("message", ""))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        ok, info = calendar_event("[selftest]", "2030-01-01", "2030-01-02")
        print("calendar:", ok, str(info)[:60])
        ok2, info2 = gmail_draft("win@aswemake.com", "[selftest]", "test")
        print("gmail draft:", ok2, str(info2)[:60])
