#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
토리 크레딧 리포트 — platform.claude.com 콘솔의 내부 JSON API를 로그인된 전용
Chrome 프로필(Playwright + 시스템 Chrome)로 읽어 'ocr' API 키의 사용량·비용·잔액을
슬랙에 보낸다. 컨테이너 비서(헤드리스)는 Chrome 을 못 띄우므로 이 잡만 호스트에서 돈다.

무인 실행: launchd(com.tory.credit-report)가 약 15초마다 'tick' 호출.
  - 평일 09:00~11:59 KST 사이 하루 1회 → ai-credit 채널 (토·일·한국 공휴일 스킵)
  - 비서 채널에서 '크레딧/비용 조회' → 컨테이너 워처가 credit-request.json 기록 → 여기서 즉시 처리

발송은 outbox 단일 관문(컨테이너 워처가 실제 전송)으로 통일. secret 은 저장하지 않는다
(브라우저 세션 쿠키는 전용 프로필 안에만 존재; 코드/로그에 키·토큰을 남기지 않는다).

사용법:
  python3 tory_credit.py login     # (최초 1회/세션만료 시) 전용 프로필에 platform.claude.com 로그인
  python3 tory_credit.py once      # 스크랩 후 리포트만 출력(발송 안 함, 테스트용)
  python3 tory_credit.py daily      # 가드 무시하고 ai-credit 으로 즉시 1회 발송(수동)
  python3 tory_credit.py tick       # launchd 진입점(요청 처리 + 일일 윈도 체크)
"""
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, date, time as dtime

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:  # py<3.9 폴백
    from datetime import timedelta, timezone
    KST = timezone(timedelta(hours=9), "KST")

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".torymemory")
STATE_DIR = os.path.join(BASE, "state")
OUTBOX_DIR = os.path.join(BASE, "outbox")
PROFILE_DIR = os.path.join(BASE, "chrome-credit")        # 로그인 유지되는 전용 프로필
REQUEST_FILE = os.path.join(STATE_DIR, "credit-request.json")
CACHE_FILE = os.path.join(STATE_DIR, "credit-usage.json")
DAILY_STATE = os.path.join(STATE_DIR, "credit-daily.json")
LOG_PREFIX = "[tory-credit]"

AI_CREDIT_CHANNEL = "C0BAW2D6AUA"      # #ai-credit
ASSIST_CHANNEL = "C0B997W7KGS"         # 승현-비서 (로그인 만료 알림용)
KEY_NAME = "ocr"                       # 보고 대상 API 키 이름(고정)
ORG_FALLBACK = "b20d6cca-bcac-4ae7-b393-6c1150cb7679"
DAILY_START = dtime(9, 0)              # 평일 발송 윈도 시작
DAILY_END = dtime(12, 0)              # 윈도 끝(이 전까지 미발송이면 발송; 늦은 기상 보정)
LOGIN_NOTICE_THROTTLE_SEC = 6 * 3600
NAV_TIMEOUT_MS = 30000

# holidays 라이브러리 없을 때를 위한 최소 고정공휴일(대체공휴일·음력은 미반영) 폴백.
_FALLBACK_FIXED = {"01-01", "03-01", "05-05", "06-06", "08-15", "10-03", "10-09", "12-25"}

SLACK_API = "https://slack.com/api/"
ENV_FILE = os.path.join(HOME, ".hermes", ".env")
BOSS = "U03EQFWTD61"
_CREDIT_NOUN = re.compile(r"(크레[디딧]트?|크래딧|credit|비용|cost|사용\s*량|usage|토큰)", re.I)
_CREDIT_VERB = re.compile(r"(조회|알려|보여|얼마|현황|리포트|report|뽑아|확인해|어때)", re.I)


def log(*a):
    print(LOG_PREFIX, datetime.now(KST).strftime("%F %T"), *a, flush=True)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def is_kr_holiday(d):
    try:
        import holidays
        return d in holidays.SouthKorea(years=d.year)
    except Exception:
        return d.strftime("%m-%d") in _FALLBACK_FIXED


# ── 스크랩 ────────────────────────────────────────────────────────────────────
_FETCH_JS = r"""
async (org) => {
  const base = "https://platform.claude.com/api";
  const pad = n => String(n).padStart(2, '0');
  const now = new Date();
  const kst = new Date(now.getTime() + 9 * 3600 * 1000);   // KST 벽시계
  const y = kst.getUTCFullYear(), m = kst.getUTCMonth(), d = kst.getUTCDate();
  const month0 = y + "-" + pad(m + 1) + "-01";
  const tmr = new Date(Date.UTC(y, m, d + 1));             // 내일(KST), ending_before 는 배타적
  const endBefore = tmr.getUTCFullYear() + "-" + pad(tmr.getUTCMonth() + 1) + "-" + pad(tmr.getUTCDate());
  const j = async (u) => {
    try {
      const r = await fetch(u, { credentials: 'include', headers: { 'accept': 'application/json' } });
      let b = null; try { b = await r.json(); } catch (e) {}
      return { status: r.status, body: b };
    } catch (e) { return { status: -1, error: String(e) }; }
  };
  const usage = await j(`${base}/organizations/${org}/usage_activities?starting_on=${month0}&ending_before=${endBefore}&categories=true&granularity=daily`);
  const cost = await j(`${base}/organizations/${org}/usage_cost?starting_on=${month0}&ending_before=${endBefore}&group_by=api_key_id`);
  const credits = await j(`${base}/organizations/${org}/prepaid/credits`);
  const keys = await j(`${base}/console/organizations/${org}/api_keys`);
  const nowIso = new Date().toISOString().slice(0, 23);
  const logs = await j(`${base}/logs/${org}/workspaces/default?page_index=0&page_size=10&max_datetime=${nowIso}`);
  return { month0, endBefore, href: location.href, usage, cost, credits, keys, logs };
}
"""


def _resolve_org(ctx):
    try:
        for c in ctx.cookies("https://platform.claude.com"):
            if c.get("name") == "lastActiveOrg" and c.get("value"):
                return c["value"]
    except Exception:
        pass
    return ORG_FALLBACK


OAI_PROJECT = "proj_vIMZUnAHdTL6LNI5C2O5p3pU"   # OpenAI ocr 프로젝트(고정)

_OAI_FETCH_JS = r"""
async (a) => {
  const now = Math.floor(Date.now()/1000);
  const d = new Date();
  const mon0 = Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1)/1000);
  const base = "https://api.openai.com/v1/dashboard/organization";
  const H = {"Authorization": a.auth, "openai-organization": a.org};
  const j = async (u) => {
    try { const r = await fetch(u, {headers: H}); let b=null; try{b=await r.json();}catch(e){} return {status:r.status, body:b}; }
    catch(e){ return {status:-1, error:String(e)}; }
  };
  const costs = await j(`${base}/costs?bucket_width=1d&start_time=${mon0}&end_time=${now}&limit=31&group_by=line_item&project_ids=${a.proj}`);
  const comp  = await j(`${base}/usage/completions?bucket_width=1d&start_time=${mon0}&end_time=${now}&limit=31&project_ids=${a.proj}`);
  return {mon0, now, costs, comp};
}
"""


def _scrape_openai(ctx):
    """platform.openai.com 새 탭에서 앱이 쓰는 Bearer 토큰을 가로채 ocr 프로젝트 비용·사용량 조회.
    토큰은 캡처만(저장 안 함). 미로그인이면 {login_needed:True}."""
    page = ctx.new_page()
    cap = {"auth": None, "org": None}

    def on_req(req):
        try:
            u = req.url
            if "api.openai.com" not in u:
                return
            if not cap["org"]:
                m = re.search(r"/organizations/(org-[A-Za-z0-9]+)", u)
                cap["org"] = (m.group(1) if m else None) or req.headers.get("openai-organization") or cap["org"]
            a = req.headers.get("authorization") or ""
            if a.lower().startswith("bearer ") and "/dashboard/" in u:
                cap["auth"] = a
        except Exception:
            pass

    page.on("request", on_req)
    try:
        page.goto("https://platform.openai.com/usage", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        for _ in range(24):   # Cloudflare 통과+토큰·org 캡처 대기(~수초); 미로그인이면 12s 후 login_needed
            if cap["auth"] and cap["org"]:
                break
            page.wait_for_timeout(500)
        if not cap["auth"]:
            return {"login_needed": True, "href": page.url}
        return page.evaluate(_OAI_FETCH_JS, {"auth": cap["auth"], "org": cap["org"], "proj": OAI_PROJECT})
    except Exception as e:
        return {"error": repr(e)[:160]}
    finally:
        try:
            page.close()
        except Exception:
            pass


def scrape():
    """전용 프로필로 Anthropic 콘솔 JSON + OpenAI 대시보드를 읽어 raw dict 반환. 예외는 호출측 처리."""
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        # OpenAI(platform.openai.com)는 Cloudflare 봇차단으로 headless 가 "잠시만 기다리십시오"에 막힌다.
        # 화면 밖(off-screen) headed 창으로 띄우면 통과한다(보이지 않음). Claude 도 같은 창에서 처리.
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, channel="chrome",
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check",
                  "--disable-background-networking", "--disable-extensions",
                  "--disable-blink-features=AutomationControlled",
                  "--window-position=-3200,-3200", "--window-size=1280,800"],
        )
        try:
            try:
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            except Exception:
                pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://platform.claude.com/usage",
                      wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            org = _resolve_org(ctx)
            data = page.evaluate(_FETCH_JS, org)
            data["org"] = org
            data["openai"] = _scrape_openai(ctx)
            return data
        finally:
            ctx.close()


def scrape_safe():
    try:
        return scrape()
    except Exception as e:
        log("scrape error:", repr(e)[:200])
        return None


def _login_needed(data):
    if not data:
        return False  # 하드 에러(크롬/락 등)는 login_needed 아님 — 재시도 대상
    cr = (data.get("credits") or {})
    if cr.get("status") in (401, 403):
        return True
    href = (data.get("href") or "")
    return "login" in href or "/console/account" in href


def summarize(data):
    """raw 콘솔 응답 → ocr 키 요약(잔액/비용/사용량, MTD + 오늘)."""
    s = {"scraped_at": datetime.now(KST).isoformat(timespec="seconds"),
         "login_needed": _login_needed(data), "ok": False}
    if not data or s["login_needed"]:
        return s
    cr = (data.get("credits") or {}).get("body") or {}
    s["balance_cents"] = cr.get("amount")
    s["currency"] = cr.get("currency", "USD")
    ar = cr.get("auto_reload_settings") or {}
    s["auto_reload"] = ar if ar.get("enabled") else None

    keys = (data.get("keys") or {}).get("body") or []
    ocr_ids = {k.get("id") for k in keys if k.get("name") == KEY_NAME}

    today = datetime.now(KST).date().isoformat()
    s["today"] = today

    usages = ((data.get("usage") or {}).get("body") or {}).get("usages") or {}
    u_mtd, u_today = defaultdict(int), defaultdict(int)
    for d, recs in usages.items():
        for r in recs:
            if r.get("key_name") != KEY_NAME:
                continue
            for f in ("input", "output", "input_cache_read",
                      "input_cache_write", "input_cache_write_1h"):
                v = r.get(f) or 0
                u_mtd[f] += v
                if d == today:
                    u_today[f] += v
    s["usage_mtd"], s["usage_today"] = dict(u_mtd), dict(u_today)

    costs = ((data.get("cost") or {}).get("body") or {}).get("costs") or {}
    c_mtd = c_today = 0
    cost_ok = bool(ocr_ids)
    for d, recs in costs.items():
        for r in recs:
            if ocr_ids and r.get("key_id") not in ocr_ids:
                continue
            t = r.get("total") or 0     # 센트 단위
            c_mtd += t
            if d == today:
                c_today += t
    s["cost_mtd_cents"] = c_mtd if cost_ok else None
    s["cost_today_cents"] = c_today if cost_ok else None

    logs_body = (data.get("logs") or {}).get("body")
    items = []
    if isinstance(logs_body, list):
        for e in logs_body[:10]:
            items.append({
                "t": e.get("request_start_time"),
                "model": e.get("model"),
                "in": e.get("prompt_token_count") or 0,
                "out": e.get("completion_token_count") or 0,
                "cache_read": e.get("prompt_token_count_cache_read") or 0,
                "latency": e.get("model_latency"),
                "error": e.get("error"),
            })
    s["logs"] = items

    # OpenAI (ocr 프로젝트) — MTD 비용·토큰·요청
    oai = data.get("openai") or {}
    so = {"login_needed": bool(oai.get("login_needed")), "ok": False}
    if not so["login_needed"] and not oai.get("error"):
        cost = 0.0
        by_model = defaultdict(float)   # line_item("모델, 작업") → 모델별 합산(Spend categories)
        for b in ((oai.get("costs") or {}).get("body") or {}).get("data", []):
            for r in b.get("results", []):
                v = (r.get("amount") or {}).get("value")
                if not v:
                    continue
                amt = float(v)
                cost += amt
                model = (r.get("line_item") or "").split(",")[0].strip()
                model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model) or "기타"
                by_model[model] += amt
        req = inp = outp = 0
        for b in ((oai.get("comp") or {}).get("body") or {}).get("data", []):
            for r in b.get("results", []):
                req += r.get("num_model_requests", 0) or 0
                inp += r.get("input_tokens", 0) or 0
                outp += r.get("output_tokens", 0) or 0
        so.update(ok=True, cost_usd=cost, requests=req, input=inp, output=outp,
                  by_model=dict(by_model))
    s["openai"] = so

    s["ok"] = True
    return s


def format_report(s, title="ocr 사용량·비용"):
    now = datetime.now(KST)
    yoil = "월화수목금토일"[now.weekday()]
    L = ["💳 *%s* · %d/%d(%s)" % (title, now.month, now.day, yoil)]

    # Anthropic (Claude)
    cm, ct = s.get("cost_mtd_cents"), s.get("cost_today_cents")
    bal = s.get("balance_cents")
    um = s.get("usage_mtd") or {}
    L.append("*Anthropic (Claude)*")
    head = []
    if cm is not None:
        head.append("이번달 *$%s* (오늘 $%s)" % (_money(cm / 100.0), _money((ct or 0) / 100.0)))
    if bal is not None:
        head.append("잔액 $%s" % _money(bal / 100.0))
    if head:
        L.append("• " + "  ·  ".join(head))
    L.append("• 입력 %s · 출력 %s 토큰" % (_int(um.get("input", 0)), _int(um.get("output", 0))))

    # OpenAI
    o = s.get("openai") or {}
    L.append("*OpenAI*")
    if o.get("login_needed"):
        L.append("• ⚠️ 재로그인 필요 — `tory_credit.py login`")
    elif not o.get("ok"):
        L.append("• (조회 실패)")
    else:
        L.append("• 이번달 *$%s*" % _money(o.get("cost_usd", 0)))
        L.append("• 입력 %s · 출력 %s 토큰 · 요청 %s"
                 % (_int(o.get("input", 0)), _int(o.get("output", 0)), _int(o.get("requests", 0))))
        bm = o.get("by_model") or {}
        if bm:
            top = sorted(bm.items(), key=lambda x: -x[1])
            parts = ["%s $%s" % (m, _money(c)) for m, c in top[:4] if c >= 0.005]
            rest = sum(c for _, c in top[4:]) + sum(c for _, c in top[:4] if c < 0.005)
            if rest >= 0.005:
                parts.append("기타 $%s" % _money(rest))
            if parts:
                L.append("• 모델별: " + " · ".join(parts))

    # 합계(이번달 비용: Claude + OpenAI)
    total = (cm / 100.0 if cm is not None else 0.0) + (o.get("cost_usd", 0) if o.get("ok") else 0.0)
    L.append("*이번달 합계: $%s*" % _money(total))

    # Anthropic 최근 로그
    logs = s.get("logs") or []
    if logs:
        models = {_short_model(e.get("model")) for e in logs}
        one = len(models) == 1
        lhead = "🧾 *Claude 최근 로그 %d건*" % len(logs)
        if one:
            lhead += " · " + next(iter(models))
        rows = []
        for e in logs:
            io = "%s→%s" % (_int(e.get("in", 0)), _int(e.get("out", 0)))
            lat = e.get("latency")
            lat_s = "%.1fs" % lat if isinstance(lat, (int, float)) else ""
            model = "" if one else _short_model(e.get("model")) + "  "
            err = e.get("error")
            errtag = "  ⚠️%s" % str(err)[:18] if (err and str(err) != "None") else ""
            rows.append("%s  %s%-13s %5s%s" % (_log_time(e.get("t")), model, io, lat_s, errtag))
        L += ["", lhead, "```\n" + "\n".join(rows) + "\n```"]

    L.append("_%d/%d %02d:%02d KST · claude + openai_" % (now.month, now.day, now.hour, now.minute))
    return "\n".join(L)


def _int(n):
    return "{:,}".format(int(n or 0))


def _money(x):
    return "{:,.2f}".format(float(x or 0))


def _short_model(m):
    m = (m or "").replace("claude-", "")
    m = re.sub(r"-\d{8}$", "", m)                       # 날짜 접미사 제거
    mm = re.match(r"^(haiku|sonnet|opus)-(.+)$", m)
    return (mm.group(1) + "-" + mm.group(2).replace("-", ".")) if mm else (m or "?")


def _log_time(t):
    try:
        return datetime.fromisoformat((t or "").replace(" ", "T")).astimezone(KST).strftime("%m/%d %H:%M")
    except Exception:
        return (t or "")[:16]


# ── 발송(outbox 단일 관문) ────────────────────────────────────────────────────
def outbox_send(channel, text, thread_ts=None):
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    msg = {"channel": channel, "text": text, "attempts": 0, "source": "credit"}
    if thread_ts:
        msg["thread_ts"] = thread_ts
    nm = "credit-%d-%d-%d.json" % (int(time.time()), os.getpid(), random.randint(1000, 9999))
    tmp = os.path.join(OUTBOX_DIR, "." + nm)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(msg, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(OUTBOX_DIR, nm))
    log("outbox queued:", nm, "->", channel)


def notify_login_needed(channel, thread_ts=None, throttle=True):
    st = read_json(DAILY_STATE, {})
    last = float(st.get("login_notice_at") or 0)
    if throttle and (time.time() - last) < LOGIN_NOTICE_THROTTLE_SEC:
        return
    outbox_send(channel,
                "⚠️ 크레딧 스크랩: platform.claude.com 세션이 만료됐어요. "
                "터미널에서 `python3 ~/.torymemory/bin/tory_credit.py login` 으로 재로그인해 주세요.",
                thread_ts)
    st["login_notice_at"] = time.time()
    write_json(DAILY_STATE, st)


# ── 모드 ─────────────────────────────────────────────────────────────────────
def handle_request(req):
    """비서 채널 온디맨드 회신 — 봇 토큰으로 그 채널·스레드에 직접 발송(채널 무관, 어느 비서든)."""
    ch = req.get("channel") or ASSIST_CHANNEL
    th = req.get("thread_ts")
    env = _load_env()
    btok = env.get("SLACK_BOT_TOKEN", "").strip() or env.get("SLACK_USER_TOKEN", "").strip()
    s = summarize(scrape_safe())
    if s.get("login_needed"):
        text = ("⚠️ 크레딧 조회 실패 — platform.claude.com 세션 만료. "
                "`python3 ~/.torymemory/bin/tory_credit.py login` 으로 재로그인이 필요합니다.")
        notify_login_needed(ASSIST_CHANNEL)
    elif not s.get("ok"):
        text = "크레딧 조회에 일시적으로 실패했어요(브라우저/네트워크). 잠시 후 다시 시도해 주세요."
    else:
        text = format_report(s, "ocr 크레딧·사용량")
        write_json(CACHE_FILE, s)
    p = {"channel": ch, "text": text, "username": "토리", "icon_emoji": ":card_index_dividers:",
         "unfurl_links": "false", "unfurl_media": "false"}
    if th:
        p["thread_ts"] = th
    _slack("chat.postMessage", btok, p, post=True)


def do_daily(force=False):
    now = datetime.now(KST)
    st = read_json(DAILY_STATE, {})
    today = now.date().isoformat()
    if not force:
        if st.get("last_sent_date") == today:
            return
        if now.weekday() >= 5:
            return
        if is_kr_holiday(now.date()):
            return
        if not (DAILY_START <= now.time() < DAILY_END):
            return
    data = scrape_safe()
    s = summarize(data)
    if s.get("login_needed"):
        notify_login_needed(ASSIST_CHANNEL)
        return  # last_sent_date 미설정 → 재로그인 후 같은 날 재시도
    if not s.get("ok"):
        log("daily skipped: scrape not ok (transient)")
        return
    outbox_send(AI_CREDIT_CHANNEL, format_report(s, "ocr 크레딧 리포트"))
    write_json(CACHE_FILE, s)
    st["last_sent_date"] = today
    st["last_ok_at"] = s.get("scraped_at")
    write_json(DAILY_STATE, st)
    log("daily posted to ai-credit")


def _load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _slack(method, token, params, post=False):
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        if post:
            req = urllib.request.Request(SLACK_API + method, data=urllib.parse.urlencode(params).encode(),
                                         headers={"Authorization": "Bearer " + token})
        else:
            req = urllib.request.Request(SLACK_API + method + "?" + urllib.parse.urlencode(params),
                                         headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception as e:
        return {"ok": False, "error": "http:%s" % e}


def _is_credit_phrase(t):
    t = (t or "").strip()
    return len(t) <= 30 and bool(_CREDIT_NOUN.search(t) and _CREDIT_VERB.search(t))


def poll_ai_credit():
    """#ai-credit 채널에서 누구든 '크레딧/비용 조회' 명령을 보내면 직접 폴링해 그 스레드에 회신.
    워처(비서 채널)와 독립이라 워처가 긴 작업 중이어도 즉시 동작. history 읽기는 user 토큰
    (bot 은 channels:history 스코프 없음), 발송·리액션은 bot 토큰(토리 명의)."""
    env = _load_env()
    rtok = env.get("SLACK_USER_TOKEN", "").strip()
    btok = env.get("SLACK_BOT_TOKEN", "").strip() or rtok
    if not rtok:
        return
    st = read_json(DAILY_STATE, {})
    last = st.get("ai_credit_last_ts")
    params = {"channel": AI_CREDIT_CHANNEL, "limit": 15}
    if last:
        params["oldest"] = last
    r = _slack("conversations.history", rtok, params)
    if not r.get("ok"):
        log("ai-credit history err:", r.get("error"))
        return
    msgs = sorted(r.get("messages", []), key=lambda m: float(m.get("ts", "0")))
    newest, serve_ts, now = last, None, time.time()
    for m in msgs:
        ts = m.get("ts")
        if not ts or (last and float(ts) <= float(last)):
            continue
        if (not newest) or float(ts) > float(newest):
            newest = ts
        if m.get("bot_id") or m.get("subtype") or not m.get("user"):  # 봇·시스템만 제외, 사람은 누구든
            continue
        if not last and (now - float(ts)) > 3600:   # 최초 실행 시 1시간 지난 과거 명령은 무시
            continue
        if _is_credit_phrase(m.get("text")):
            serve_ts = ts
    if newest and newest != last:
        st["ai_credit_last_ts"] = newest
        write_json(DAILY_STATE, st)
    if not serve_ts:
        return
    log("serving #ai-credit on-demand", serve_ts)
    _slack("reactions.add", btok, {"channel": AI_CREDIT_CHANNEL, "timestamp": serve_ts, "name": "eyes"}, post=True)
    s = summarize(scrape_safe())
    if s.get("login_needed"):
        text = "⚠️ 세션 만료 — `python3 ~/.torymemory/bin/tory_credit.py login` 으로 재로그인이 필요합니다."
    elif not s.get("ok"):
        text = "크레딧 조회에 일시적으로 실패했어요. 잠시 후 다시 시도해 주세요."
    else:
        text = format_report(s, "ocr 크레딧·사용량")
        write_json(CACHE_FILE, s)
    _slack("chat.postMessage", btok,
           {"channel": AI_CREDIT_CHANNEL, "thread_ts": serve_ts, "text": text,
            "username": "토리", "icon_emoji": ":card_index_dividers:",
            "unfurl_links": "false", "unfurl_media": "false"}, post=True)
    _slack("reactions.remove", btok, {"channel": AI_CREDIT_CHANNEL, "timestamp": serve_ts, "name": "eyes"}, post=True)


def _request_files():
    """tory + 모든 비서(assistants/*/state)의 credit-request.json 경로."""
    files = [REQUEST_FILE] if os.path.exists(REQUEST_FILE) else []
    adir = os.path.join(BASE, "assistants")
    try:
        for name in os.listdir(adir):
            p = os.path.join(adir, name, "state", "credit-request.json")
            if os.path.exists(p):
                files.append(p)
    except FileNotFoundError:
        pass
    return files


def tick():
    os.makedirs(STATE_DIR, exist_ok=True)
    for rf in _request_files():
        req = read_json(rf, None)
        if not req:
            continue
        try:
            os.remove(rf)
        except FileNotFoundError:
            pass
        log("serving on-demand request (비서 채널)", req.get("channel"))
        handle_request(req)
    poll_ai_credit()
    do_daily()


def login(timeout=300):
    """전용 프로필에 platform.claude.com + platform.openai.com 로그인(헤드풀). 둘 다 되면 자동 감지."""
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, channel="chrome",
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check",
                  "--disable-blink-features=AutomationControlled"])
        try:
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        except Exception:
            pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://platform.claude.com/usage", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        oai_page = ctx.new_page()
        cap = {"auth": None}

        def _oai_cap(r):
            try:
                if "api.openai.com" in r.url and "/dashboard/" in r.url:
                    a = r.headers.get("authorization") or ""
                    if a.lower().startswith("bearer "):
                        cap["auth"] = a
            except Exception:
                pass

        oai_page.on("request", _oai_cap)
        oai_page.goto("https://platform.openai.com/usage", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            oai_page.bring_to_front()
        except Exception:
            pass
        print("OpenAI 탭(platform.openai.com)에서 로그인하세요. Claude 탭은 자동 통과. 최대 %d초 대기…" % timeout, flush=True)
        # 새로고침하지 않는다(로그인 입력 방해 방지) — 로그인 후 /usage 가 뜨면 토큰이 수동 캡처된다.
        t0 = time.time()
        claude_ok = False
        while time.time() - t0 < timeout:
            try:
                if not claude_ok:
                    org = _resolve_org(ctx)
                    d = page.evaluate(
                        "async (org) => { const r = await fetch("
                        "`https://platform.claude.com/api/organizations/${org}/prepaid/credits`,"
                        "{credentials:'include'}); return r.status; }", org)
                    claude_ok = (d == 200)
            except Exception:
                pass
            if claude_ok and cap["auth"]:
                break
            time.sleep(3)
        oai_ok = bool(cap["auth"])
        print("로그인 상태 — Claude: %s · OpenAI: %s" %
              ("OK" if claude_ok else "미완료", "OK" if oai_ok else "미완료"), flush=True)
        ctx.close()
        return 0 if (claude_ok and oai_ok) else 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if mode == "tick":
        tick()
    elif mode == "login":
        return login()
    elif mode == "daily":
        do_daily(force=True)
    elif mode == "once":
        data = scrape_safe()
        s = summarize(data)
        if s.get("login_needed"):
            print("LOGIN_NEEDED — run: tory_credit.py login")
            return 2
        if not s.get("ok"):
            print("SCRAPE_FAILED (transient)")
            return 1
        print(format_report(s, "ocr 크레딧·사용량"))
        print("\n[raw]", json.dumps(s, ensure_ascii=False))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
