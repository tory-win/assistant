#!/usr/bin/env python3
"""
torymemory_google_fetch.py — 토리 비서: Gmail·Calendar·Drive 읽기전용 폴러.

토큰(~/.hermes/google_token[_account].json, refresh 자동)으로 회사 Google 을 읽어:
 - Gmail: 최근 INBOX 스레드 중 '내가 마지막에 답 안 한' 것 → 주의 큐(kind=email)
 - Calendar: 오늘+내일 일정 → 컨텍스트(브리핑 '오늘 일정' 섹션용)
 - Drive: 최근 수정 문서 → 컨텍스트
 - feed(redact, scope=company): 메일 제목·일정 → 큐레이터가 대외비 기억으로 정리

회사 계정이므로 전부 대외비(awm_confidential) 경계. 읽기전용. hermes venv python 으로 실행.
  torymemory_google_fetch.py [--account company|personal] [--dry-run]
"""
import argparse
import datetime
import json
import os
import re
import signal
import socket
import sys

# launchd 세션에서 DNS/소켓이 가끔 무한 대기 → 매 호출 30s 타임아웃 + 전체 120s 워치독.
socket.setdefaulttimeout(30)

# 모든 시각은 한국시간(KST) 고정 — 머신 tz 에 의존하지 않는다.
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = datetime.timezone(datetime.timedelta(hours=9), "KST")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
SHIM_BIN = os.path.expanduser("~/.torymemory/bin")
if SHIM_BIN != SCRIPT_DIR and SHIM_BIN not in sys.path:
    sys.path.append(SHIM_BIN)
try:
    from torymemory_redact_secrets import redact
except Exception:
    def redact(t):
        return t

from google.oauth2.credentials import Credentials  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

HOME = os.path.expanduser("~")
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

BASE_DIR = PROFILE.get("base_dir") or os.path.join(HOME, ".torymemory")
STATE_DIR = PROFILE.get("state_dir") or os.path.join(HOME, ".torymemory", "state")
FEED_DIR = (PROFILE.get("feed_dirs") or {}).get("google") or os.path.join(HOME, ".torymemory", "feeds", "google")
CONFIG_FILE = PROFILE.get("slack_config_file") or os.path.join(HOME, ".torymemory", "slack-config.json")
GOOGLE_TOKEN_DIR = PROFILE.get("google_token_dir") or os.path.dirname(PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env"))
GOOGLE_TOKEN_FILE = PROFILE.get("google_token_file") or ""
ATTN_FILE = os.path.join(STATE_DIR, "google-attention.json")
CTX_FILE = os.path.join(STATE_DIR, "google-context.json")
URGENT_KW = ("긴급", "ASAP", "오늘까지", "데드라인", "마감", "지금", "urgent")
LOG = "[google-fetch]"
ENABLED_SOURCES = set(PROFILE.get("enabled_sources") or
                      ["slack", "gmail", "calendar", "drive", "notion", "memory", "local", "recordings"])


def log(*a):
    print(LOG, *a, file=sys.stderr, flush=True)


def token_path(account):
    if account == "company" and GOOGLE_TOKEN_FILE:
        return os.path.expanduser(GOOGLE_TOKEN_FILE)
    suffix = "" if account == "company" else "_" + account
    return os.path.join(os.path.expanduser(GOOGLE_TOKEN_DIR), "google_token%s.json" % suffix)


def load_creds(account):
    p = token_path(account)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        tok = json.load(f)
    creds = Credentials(token=tok.get("token"), refresh_token=tok.get("refresh_token"),
                        token_uri=tok.get("token_uri"), client_id=tok.get("client_id"),
                        client_secret=tok.get("client_secret"), scopes=tok.get("scopes"))
    if not creds.valid:
        creds.refresh(Request())
        tok["token"] = creds.token
        tok["expiry"] = creds.expiry.isoformat() if getattr(creds, "expiry", None) else None
        # 생성 시점부터 600 보장(쓰기 후 chmod 사이의 노출 창 제거)
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(p + ".tmp", p)
    return creds


_LOCK_FH = None


def single_instance(name):
    """launchd 주기와 수동 실행 중복 차단."""
    global _LOCK_FH
    import fcntl
    _LOCK_FH = open(os.path.join(STATE_DIR, name + ".lock"), "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"ok": True, "skip": "already_running"}))
        sys.exit(0)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


FILTER_FILE = PROFILE.get("mail_filters_file") or os.path.join(BASE_DIR, "mail-filters.json")


def _dec_hdr(s):
    """RFC2047 인코딩(=?UTF-8?B?..?=) 헤더 디코드 — 한국어·일본어 표시명/제목이
    인코딩된 채 오면 '뉴스레터·(광고)' 패턴 필터가 전부 무력화되는 버그 수정(2026-06-11)."""
    if not s or "=?" not in s:
        return s
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def load_mail_filters():
    try:
        with open(FILTER_FILE) as f:
            d = json.load(f)
        return ([x.lower() for x in d.get("block_senders", [])],
                [x.lower() for x in d.get("block_subjects", [])])
    except Exception:
        return [], []


def gmail_needs_reply(svc, max_threads):
    prof = svc.users().getProfile(userId="me").execute()
    me = (prof.get("emailAddress") or "").lower()
    blk_from, blk_subj = load_mail_filters()
    # Workspace 계정엔 탭(CATEGORY_*) 라벨이 없어 category:primary 가 항상 0건 → fail-open 으로 교체.
    # 탭 미사용 계정에선 -category:* 가 no-op, 탭 사용 계정에선 홍보·소셜·업데이트·포럼만 제외.
    # (광고성 발신자 제외는 아래 automated 휴리스틱이 담당)
    threads = svc.users().threads().list(
        userId="me",
        q="in:inbox newer_than:7d -category:promotions -category:social -category:updates -category:forums",
        maxResults=max_threads).execute().get("threads", [])
    items, feed = [], []
    for th in threads:
        try:
            full = svc.users().threads().get(userId="me", id=th["id"], format="metadata",
                                             metadataHeaders=["From", "Subject", "List-Unsubscribe"]).execute()
        except Exception:
            continue
        msgs = full.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        hdrs = {h["name"]: h["value"] for h in last.get("payload", {}).get("headers", [])}
        frm = _dec_hdr(hdrs.get("From", ""))
        subj = redact(_dec_hdr(hdrs.get("Subject", "(제목 없음)")))
        snippet = redact(last.get("snippet", ""))
        ts = last.get("internalDate", "0")
        from_me = me and me in frm.lower()
        feed.append({"v": 1, "kind": "google_msg", "host": "gmail", "scope": "company",
                     "channel_id": th["id"], "channel": "Gmail", "user": frm[:80],
                     "ts": ts, "text": "[메일] %s — %s" % (subj, snippet)})
        low = frm.lower()
        # 표시이름(따옴표 안 한국어 이름)도 본다 — 깨끗한 From 주소를 쓰는 뉴스레터/광고 발신자 포착.
        mdisp = re.match(r'\s*"?([^"<]+?)"?\s*<', frm)
        disp = (mdisp.group(1).strip().lower() if mdisp else "")
        # List-Unsubscribe 헤더 = 자동발송(뉴스레터·마케팅)의 가장 신뢰도 높은 신호.
        has_list_unsub = bool(hdrs.get("List-Unsubscribe"))
        automated = (
            has_list_unsub
            or any(x in low for x in ("noreply", "no-reply", "no_reply", "donotreply",
                                      "newsletter", "mailer-daemon", "notification", "mailchimp",
                                      "stibee", "mailerlite"))
            or any(x in disp for x in ("뉴스레터", "newsletter", "광고", "프로모션", "promotion",
                                       "no-reply", "noreply", "세미나", "セミナー", "webinar",
                                       "마케팅", "이벤트", "events"))
            or any(b in low or b in disp for b in blk_from)
            or any(b in subj.lower() for b in blk_subj)
        )
        # 정보통신망법 광고표기: 제목이 (광고)/[광고] 로 시작 → 답장 대상 아님(feed 적재는 유지).
        ad_subject = bool(re.match(r'\s*[\(\[（［]\s*광고\s*[\)\]）］]', subj))
        # automated·광고는 needs_reply 에서 제외 → 아래 urgent 계산에 도달하지 않아 오탐도 막힌다.
        if not from_me and not automated and not ad_subject:
            txt = "%s — %s" % (subj, snippet)
            kw = next((k for k in URGENT_KW if k.lower() in txt.lower()), None)
            items.append({
                "kind": "email", "urgent": bool(kw), "channel_id": th["id"], "channel": "Gmail",
                "user": re.sub(r"\s*<[^>]+>", "", frm)[:60] or frm[:60],
                "ts": str(int(ts) / 1000.0) if ts.isdigit() else "0",  # 비정상 ts 는 0 — brief 정렬·필터가 안전하게 처리
                "iso": datetime.datetime.fromtimestamp(int(ts) / 1000.0, tz=KST).isoformat(timespec="seconds") if ts.isdigit() else "",
                "permalink": "https://mail.google.com/mail/u/0/#inbox/%s" % th["id"],
                "keyword": kw, "excerpt": (txt[:280] + "…") if len(txt) > 280 else txt,
            })
    return items, feed


def calendar_today_tomorrow(svc):
    # '오늘 남은 시간 + 내일 전체'를 한국시간 기준으로 자른다(UTC 로 자르면 경계가 9시간 밀린다).
    now = datetime.datetime.now(KST)
    tmin = now.isoformat()
    tmax = (now + datetime.timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    # primary 만 보던 것 → 연결된 모든 캘린더(동료 공유 포함)를 순회한다. 회사 회의가 동료
    # 공유 캘린더에 있어 primary=0건이라 회의를 통째로 놓치던 버그 수정(2026-06-11).
    try:
        cal_ids = [c.get("id") for c in svc.calendarList().list().execute().get("items", []) if c.get("id")]
    except Exception:
        cal_ids = ["primary"]
    evs, seen_ev = [], set()
    for cid in cal_ids:
        try:
            items = svc.events().list(calendarId=cid, timeMin=tmin, timeMax=tmax, singleEvents=True,
                                      orderBy="startTime", maxResults=25,
                                      timeZone="Asia/Seoul").execute().get("items", [])
        except Exception:
            continue
        for e in items:
            eid = e.get("id", "")
            if eid and eid in seen_ev:   # 공유 캘린더 간 중복 일정 1개로
                continue
            seen_ev.add(eid)
            evs.append(e)
    evs.sort(key=lambda e: e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or "")
    out, feed = [], []
    for e in evs:
        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""
        summary = redact(e.get("summary", "(제목 없음)"))
        atts = [a.get("email") for a in e.get("attendees", []) if a.get("email")]
        out.append({"start": start, "summary": summary, "attendees": atts[:8],
                    "location": e.get("location", ""), "link": e.get("htmlLink", "")})
        feed.append({"v": 1, "kind": "google_msg", "host": "calendar", "scope": "company",
                     "channel_id": e.get("id", ""), "channel": "Calendar", "user": "",
                     "ts": "0", "text": "[일정] %s @ %s" % (summary, start)})
    return out, feed


def drive_recent(svc, n):
    fs = svc.files().list(orderBy="modifiedTime desc", pageSize=n, corpora="user",
                          q="trashed=false",
                          fields="files(name,modifiedTime,webViewLink,mimeType)").execute().get("files", [])
    return [{"name": redact(f.get("name", "")), "modified": f.get("modifiedTime", ""),
             "link": f.get("webViewLink", "")} for f in fs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="company", choices=["company", "personal"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not ({"gmail", "calendar", "drive"} & ENABLED_SOURCES):
        print(json.dumps({"ok": True, "skip": "google_sources_disabled"}))
        return 0

    os.makedirs(STATE_DIR, exist_ok=True)
    single_instance("google-fetch")

    # 워치독: 어떤 API 호출도 전체 120s 넘기면 중단(launchd 슬롯 보호). main-thread 에서만 동작.
    try:
        signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError("watchdog 120s")))
        signal.alarm(120)
    except Exception:
        pass

    creds = load_creds(args.account)
    if not creds:
        log("no token for", args.account, "→ nothing to do")
        print(json.dumps({"ok": False, "reason": "no_token"}))
        return 0

    # static_discovery=True → 패키지 내장 디스커버리 사용(network 디스커버리 fetch 회피, build 는 무네트워크).
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False, static_discovery=True)
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False, static_discovery=True)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False, static_discovery=True)

    email_items, email_feed = gmail_needs_reply(gmail, 25)
    events, cal_feed = calendar_today_tomorrow(cal)
    files = drive_recent(drive, 8)
    try:
        signal.alarm(0)  # 네트워크 끝 → 워치독 해제(로컬 쓰기는 중단 안 됨)
    except Exception:
        pass

    summary = {"ok": True, "email_needs_reply": len(email_items), "events": len(events),
               "drive_recent": len(files), "account": args.account,
               "ts": datetime.datetime.now(KST).isoformat(timespec="seconds")}

    if args.dry_run:
        log("dry-run:", json.dumps(summary, ensure_ascii=False))
        print(json.dumps({"summary": summary,
                          "sample_email": email_items[:3], "events": events[:5], "drive": files[:5]},
                         ensure_ascii=False, indent=1))
        return 0

    _write_json(ATTN_FILE, {"_ts": datetime.datetime.now().timestamp(), "items": email_items})
    _write_json(CTX_FILE, {"_ts": datetime.datetime.now().timestamp(), "calendar": events, "drive": files})

    # feed (scope=company → 큐레이터가 awm_confidential 로 적재). 제목·일정만, 본문 X.
    os.makedirs(FEED_DIR, exist_ok=True)
    fp = os.path.join(FEED_DIR, "%s.jsonl" % args.account)
    lines = [json.dumps(r, ensure_ascii=False) for r in (cal_feed + email_feed)]
    if lines:
        with open(fp, "a") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(fp, 0o600)

    log("cycle:", json.dumps(summary, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("error:", repr(e))
        print(json.dumps({"ok": False, "error": repr(e)[:120]}))
        sys.exit(0)
