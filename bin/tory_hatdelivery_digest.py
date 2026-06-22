#!/usr/bin/env python3
"""Separate HatDelivery business digest from Notion feed.

Sends only to the main Tory assistant channel. It never runs for other
assistant profiles, even if the same script is mounted into their container.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9), "KST")

HOME = os.path.expanduser("~")
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    assistant_config = None
    PROFILE = {}

ASSISTANT_ID = PROFILE.get("id") or os.environ.get("TORY_ASSISTANT_ID", "tory").strip() or "tory"
ENV_FILE = PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env")
CONFIG_FILE = PROFILE.get("slack_config_file") or os.path.join(HOME, ".torymemory", "slack-config.json")
STATE_DIR = PROFILE.get("state_dir") or os.path.join(HOME, ".torymemory", "state")
NOTION_DIR = (PROFILE.get("feed_dirs") or {}).get("notion") or os.path.join(HOME, ".torymemory", "feeds", "notion")
STATE_FILE = os.path.join(STATE_DIR, "hatdelivery-digest.json")
SLACK_API = "https://slack.com/api/"
MAIN_ASSISTANT_ID = "tory"
MAIN_CHANNEL_ID = "C0B997W7KGS"
LOG = "[hatdelivery-digest]"

DEFAULTS = {
    "hatdelivery_digest": {
        "enabled": True,
        "assistant_ids": ["tory"],
        "channel_id": MAIN_CHANNEL_ID,
        "lookback_days": 7,
        "min_interval_seconds": 21600,
        "max_docs": 24,
        "model": "gpt-5.4",
    }
}

KEYWORDS = [
    "햇배달", "영수증 ocr", "ocr", "기사 앱", "기사앱", "배송 기사", "배송기사",
    "배달완료", "배달 완료", "평균 배달시간", "배달시간", "배달 요일",
    "다다익스", "바로고", "홈마트 신천점", "전주마트 효자점", "시흥 홈마트",
    "마트별 배달", "배달 요청사항", "배달 정보",
]


def log(*parts):
    print(LOG, *parts, file=sys.stderr, flush=True)


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def parse_ts(rec, fallback=0.0):
    iso = rec.get("iso") or ""
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    try:
        ts = float(rec.get("ts") or 0)
        return ts / 1000 if ts > 1e12 else ts
    except Exception:
        return fallback


def clean_text(text, limit=900):
    text = html.unescape(str(text or ""))
    text = re.sub(r"https://www\\.notion\\.so/\\S+", "", text)
    text = re.sub(r"\\s+", " ", text).strip()
    return (text[:limit - 1] + "…") if len(text) > limit else text


def notion_url(page_id):
    pid = (page_id or "").replace("-", "")
    return "https://www.notion.so/aswemake/%s" % pid if pid else ""


def is_hatdelivery(rec):
    blob = " ".join(str(rec.get(k) or "") for k in ("channel", "text")).lower()
    if "데이터 마트" in blob or "data mart" in blob:
        blob = blob.replace("데이터 마트", "").replace("data mart", "")
    if "햇배달" in blob:
        return True
    if "배달" in blob and any(k in blob for k in ("기사", "ocr", "영수증", "시간", "요일", "바로고",
                                                "다다익스", "지입", "마트별 배달")):
        return True
    return any(k.lower() in blob for k in KEYWORDS)


def score_doc(rec):
    blob = " ".join(str(rec.get(k) or "") for k in ("channel", "text")).lower()
    score = 0
    for kw in KEYWORDS:
        if kw.lower() in blob:
            score += 2 if len(kw) >= 4 else 1
    if any(k in blob for k in ("이슈", "문제현상", "버그", "리스크", "우려", "블락")):
        score += 4
    if any(k in blob for k in ("결론", "확정", "결정", "합의", "다음", "todo", "요청")):
        score += 3
    return score


def load_notion_docs(lookback_days=7, max_docs=24):
    cutoff = time.time() - max(1, float(lookback_days)) * 86400
    files = []
    if os.path.isdir(NOTION_DIR):
        for name in os.listdir(NOTION_DIR):
            path = os.path.join(NOTION_DIR, name)
            if not name.endswith(".jsonl") or not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff - 86400:
                    files.append(path)
            except OSError:
                pass
    latest = {}
    for path in sorted(files):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("host") != "notion" or not is_hatdelivery(rec):
                continue
            ts = parse_ts(rec, os.path.getmtime(path))
            if ts < cutoff:
                continue
            pid = rec.get("channel_id") or rec.get("channel") or hashlib.sha1(line.encode()).hexdigest()
            rec["_ts"] = ts
            rec["_score"] = score_doc(rec)
            prev = latest.get(pid)
            if not prev or ts >= prev.get("_ts", 0):
                latest[pid] = rec
    docs = sorted(latest.values(), key=lambda r: (r.get("_score", 0), r.get("_ts", 0)), reverse=True)
    return docs[:max_docs]


def digest_hash(docs):
    body = "\n".join("%s|%s|%s" % (d.get("channel_id"), d.get("iso"), d.get("text")) for d in docs)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def llm(messages, key, base, model):
    body = {"model": model, "messages": messages, "stream": False}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=data,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    return out.get("choices", [{}])[0].get("message", {}).get("content", "").strip()


def fallback_digest(docs):
    lines = [
        "*핵심 요약*",
    ]
    for d in docs[:3]:
        lines.append("- %s" % clean_text(d.get("text"), 180))
    issue_docs = [d for d in docs if any(k in (d.get("text") or "") for k in ("문제현상", "버그", "음량", "우려", "리스크"))]
    if issue_docs:
        lines += ["", "*이슈/리스크*"]
        for d in issue_docs[:4]:
            lines.append("- %s" % clean_text(d.get("text"), 170))
    progress_docs = [d for d in docs if any(k in (d.get("text") or "") for k in ("결론", "합의", "진행", "공유", "도입"))]
    if progress_docs:
        lines += ["", "*진행/결정*"]
        for d in progress_docs[:3]:
            lines.append("- %s" % clean_text(d.get("text"), 170))
    lines += ["", "*내가 볼 액션*", "- 아래 근거 문서 중 미팅/이슈 문서에서 의사결정 필요한 항목만 확인"]
    lines += ["", "*근거 문서*"]
    for d in docs[:8]:
        url = notion_url(d.get("channel_id"))
        title = d.get("channel") or "Notion"
        lines.append("- <%s|%s>" % (url, title) if url else "- %s" % title)
    return "\n".join(lines)


def compose_digest(docs, env, cfg):
    hcfg = cfg.get("hatdelivery_digest") or {}
    sources = []
    for i, d in enumerate(docs, 1):
        when = ""
        try:
            when = datetime.fromtimestamp(d.get("_ts", 0), tz=KST).strftime("%m/%d %H:%M")
        except Exception:
            pass
        sources.append("%d. [%s] %s\n%s\nlink: %s" % (
            i, when, d.get("channel") or "Notion", clean_text(d.get("text"), 850),
            notion_url(d.get("channel_id"))))
    prompt = (
        "아래 Notion 문서 발췌만 근거로 햇배달 사업 현황을 Slack mrkdwn으로 정리해라.\n"
        "절대 지어내지 말고, 문서에 없는 수치/일정/담당자는 쓰지 마라.\n"
        "형식은 정확히 다음 섹션만 사용한다. 전체 1,500자 이내.\n"
        "*핵심 요약* 2-4줄\n"
        "*이슈/리스크* 중요한 것 우선 3-5개\n"
        "*진행/결정* 확인된 결정 또는 진행 2-4개\n"
        "*내가 볼 액션* 오승현이 봐야 할 의사결정/확인 1-4개\n"
        "*근거 문서* 주요 문서 링크 4-7개\n\n"
        "Notion 발췌:\n" + "\n\n".join(sources)
    )
    key = env.get("OPENAI_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL") or env.get("OPENAI_BASE_URL", "http://localhost:8321/v1")
    model = hcfg.get("model") or env.get("TORY_DIGEST_MODEL") or "gpt-5.4"
    if not key:
        return fallback_digest(docs)
    try:
        out = llm([
            {"role": "system", "content": "너는 ASWEMAKE 대표를 보좌하는 사업 요약 비서다. 한국어로 짧고 정확하게 쓴다."},
            {"role": "user", "content": prompt},
        ], key, base, model)
        return out or fallback_digest(docs)
    except Exception as e:
        log("compose fallback:", repr(e))
        return fallback_digest(docs)


def slack_post(token, channel, text):
    params = {
        "channel": channel,
        "text": text,
        "username": PROFILE.get("assistant_name") or "토리",
        "icon_emoji": PROFILE.get("slack_icon_emoji") or ":card_index_dividers:",
        "unfurl_links": "false",
        "unfurl_media": "false",
    }
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API + "chat.postMessage",
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if assistant_config:
        assistant_config.ensure_profile_dirs(PROFILE)
    cfg = load_config()
    hcfg = cfg.get("hatdelivery_digest") or {}
    allowed = hcfg.get("assistant_ids") or [MAIN_ASSISTANT_ID]
    channel = hcfg.get("channel_id") or MAIN_CHANNEL_ID
    if ASSISTANT_ID not in allowed or ASSISTANT_ID != MAIN_ASSISTANT_ID:
        print(json.dumps({"ok": True, "skip": "wrong_assistant", "assistant": ASSISTANT_ID}, ensure_ascii=False))
        return 0
    if not hcfg.get("enabled", True):
        print(json.dumps({"ok": True, "skip": "disabled"}, ensure_ascii=False))
        return 0
    if channel != MAIN_CHANNEL_ID:
        print(json.dumps({"ok": False, "reason": "channel_guard", "channel": channel}, ensure_ascii=False))
        return 0

    docs = load_notion_docs(hcfg.get("lookback_days", 7), int(hcfg.get("max_docs", 24)))
    if not docs:
        print(json.dumps({"ok": True, "skip": "no_hatdelivery_notion_docs"}, ensure_ascii=False))
        return 0
    dhash = digest_hash(docs)
    state = read_json(STATE_FILE, {})
    min_interval = float(hcfg.get("min_interval_seconds", 21600))
    due = args.force or dhash != state.get("digest_hash") and time.time() - float(state.get("last_sent", 0) or 0) >= min_interval
    env = load_env(ENV_FILE)
    body = compose_digest(docs, env, cfg)
    title = "*햇배달 사업 요약* · %s KST" % datetime.now(KST).strftime("%m/%d %H:%M")
    text = title + "\n" + body.strip()
    if args.dry_run:
        print(text)
        print(json.dumps({"ok": True, "dry_run": True, "docs": len(docs), "digest_hash": dhash, "due": due}, ensure_ascii=False))
        return 0
    if not due:
        print(json.dumps({"ok": True, "skip": "unchanged", "docs": len(docs), "digest_hash": dhash}, ensure_ascii=False))
        return 0
    token = env.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        print(json.dumps({"ok": False, "reason": "no_bot_token"}, ensure_ascii=False))
        return 0
    res = slack_post(token, channel, text)
    ok = bool(res.get("ok"))
    if ok:
        write_json(STATE_FILE, {"digest_hash": dhash, "last_sent": time.time(),
                                "last_ts": res.get("ts"), "docs": len(docs)})
    print(json.dumps({"ok": ok, "error": res.get("error"), "docs": len(docs), "channel": channel}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
