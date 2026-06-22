#!/usr/bin/env python3
"""
torymemory_slack_fetch.py — headless Slack poller for the Hermes 비서.

Polls the user's COMPANY Slack (ASWEMAKE) via a user token, redacts secrets,
appends harvest-compatible JSONL to ~/.torymemory/feeds/slack/<channel>.jsonl,
and maintains an attention queue (DMs / @mentions / keyword hits) at
~/.torymemory/state/slack-attention.json for the briefing step.

CONFIDENTIAL BOUNDARY: this is company Slack. Every emitted record is
scope="company" so the Hermes curator routes it to user_id=awm_confidential,
never personal `tory`. (지침 9_user_저장경계)

Design mirrors the existing feed scripts: stdlib only, fail-closed redaction,
cursor-based incremental reads (per-channel last_ts), safe to run every ~150s
from launchd. Reads token from ~/.hermes/.env (SLACK_USER_TOKEN); if absent it
exits cleanly so launchd does not error-spam before the token is provisioned.

  torymemory_slack_fetch.py              # one poll cycle (default)
  torymemory_slack_fetch.py --discover   # refresh conversation discovery only
  torymemory_slack_fetch.py --dry-run    # poll but write nothing
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# 모든 표기 시각은 한국시간(KST) 고정 — 머신 tz 에 의존하지 않는다.
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    from datetime import timedelta as _td
    KST = timezone(_td(hours=9), "KST")

HOME = os.path.expanduser("~")
REPO_SCRIPTS = "/Users/tory/Downloads/dev/torymemory/api/scripts"
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    assistant_config = None
    PROFILE = {}

BASE_DIR = PROFILE.get("base_dir") or os.path.join(HOME, ".torymemory")
ENV_FILE = PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env")
CONFIG_FILE = PROFILE.get("slack_config_file") or os.path.join(HOME, ".torymemory", "slack-config.json")
FEED_DIR = (PROFILE.get("feed_dirs") or {}).get("slack") or os.path.join(HOME, ".torymemory", "feeds", "slack")
STATE_DIR = PROFILE.get("state_dir") or os.path.join(HOME, ".torymemory", "state")
CURSOR_FILE = os.path.join(STATE_DIR, "slack-cursor.json")
ATTENTION_FILE = os.path.join(STATE_DIR, "slack-attention.json")
USERS_CACHE = os.path.join(STATE_DIR, "slack-users.json")
DISCOVERED_FILE = os.path.join(STATE_DIR, "slack-discovered.json")
SLACK_API = "https://slack.com/api/"
LOG = "[slack-fetch]"

DEFAULTS = {
    "briefing": {"destination": "self_dm", "private_channel_id": "", "use_canvas": True, "urgent_dm_ping": True},
    "triage": {"watch_dms": True, "watch_mentions": True, "key_channels": [],
               "keywords": ["긴급", "ASAP", "오늘까지", "내일까지", "데드라인", "마감", "승인", "검토 부탁", "확인 부탁", "리뷰 부탁", "회신"]},
    "memory": {"ingest_scope": "member", "user_id": "awm_confidential", "min_chars": 0},
    "poll": {"interval_seconds": 150, "first_run_lookback_hours": 24, "max_messages_per_channel": 200, "max_channels": 80},
    "exclude_channels": [],
}
# keywords that escalate an attention item to "urgent"
URGENT_KW = ("긴급", "ASAP", "오늘까지", "데드라인", "마감", "지금")


# ── redaction: local TCC-safe copy → inline fallback ──
# ~/Downloads(REPO_SCRIPTS)는 TCC 보호 → launchd 에서 sys.path 에 두면 import 시 opendir 영구정지.
# TCC-safe 사본(이 스크립트 폴더)만 쓰고, 없으면 인라인 fallback.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from torymemory_redact_secrets import redact  # noqa: E402
except Exception:
    _RX = [
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED]"),
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"), "bearer [REDACTED]"),
        (re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}\b", ), "[REDACTED]"),
        (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|xox[baprs]|glpat)[-_][A-Za-z0-9._\-]{12,}\b"), "[REDACTED]"),
        (re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\b"), "[REDACTED]"),
    ]

    def redact(t):  # type: ignore
        if not t:
            return t
        try:
            s = str(t)
            for rx, rep in _RX:
                s = rx.sub(rep, s)
            return s
        except Exception:
            return "[REDACTED]"


def log(*a):
    print(LOG, *a, file=sys.stderr, flush=True)


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log("config parse error, using defaults:", repr(e))
    if PROFILE.get("assistant_channel_id") and not cfg.get("briefing", {}).get("private_channel_id"):
        cfg.setdefault("briefing", {})["private_channel_id"] = PROFILE["assistant_channel_id"]
    if PROFILE.get("memory_user_id"):
        cfg.setdefault("memory", {})["user_id"] = PROFILE["memory_user_id"]
    return cfg


def assistant_output_channels():
    """All assistant output/control channels. Slack fetch should not ingest another assistant's inbox."""
    channels = set()
    try:
        if assistant_config:
            ch = (getattr(assistant_config, "BASE_DEFAULT", {}) or {}).get("assistant_channel_id")
            if ch:
                channels.add(ch)
            root = getattr(assistant_config, "ASSISTANTS_DIR", os.path.join(HOME, ".torymemory", "assistants"))
        else:
            root = os.path.join(HOME, ".torymemory", "assistants")
        for name in os.listdir(root):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    prof = json.load(f)
                ch = prof.get("assistant_channel_id")
                if ch:
                    channels.add(ch)
            except Exception:
                pass
    except Exception:
        pass
    return channels


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0)
    os.chmod(tmp, 0o600)  # 회사 메시지 발췌가 들어가는 상태 파일 — 소유자 외 읽기 금지
    os.replace(tmp, path)


_LOCK_FH = None


def single_instance(name):
    """launchd 주기와 수동 실행이 겹쳐 같은 상태 파일을 동시에 쓰는 것을 차단."""
    global _LOCK_FH
    import fcntl
    _LOCK_FH = open(os.path.join(STATE_DIR, name + ".lock"), "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"ok": True, "skip": "already_running"}))
        sys.exit(0)


# ── Slack Web API ──
def slack_call(method, token, params=None, post=False):
    params = params or {}
    url = SLACK_API + method
    headers = {"Authorization": "Bearer " + token}
    data = None
    if post:
        data = urllib.parse.urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    for _ in range(5):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if post else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "3"))
                log("429 rate-limited on", method, "→ sleep", wait)
                time.sleep(wait)
                continue
            log("HTTP", e.code, "on", method)
            return {"ok": False, "error": "http_%d" % e.code}
        except Exception as e:
            log("net error on", method, repr(e))
            return {"ok": False, "error": "neterr"}
        if not body.get("ok") and body.get("error") == "ratelimited":
            time.sleep(2)
            continue
        return body
    return {"ok": False, "error": "retries_exhausted"}


def paginate(method, token, params, list_key, cap=5000):
    out, cur = [], None
    while True:
        p = dict(params)
        if cur:
            p["cursor"] = cur
        body = slack_call(method, token, p)
        if not body.get("ok"):
            log(method, "error:", body.get("error"))
            break
        out.extend(body.get(list_key, []) or [])
        cur = (body.get("response_metadata") or {}).get("next_cursor")
        if not cur or len(out) >= cap:
            break
    return out


# ── user name cache ──
def load_users(token, max_age=21600):
    cache = _read_json(USERS_CACHE, None)
    if cache and (time.time() - cache.get("_ts", 0)) < max_age:
        return cache.get("map", {})
    members = paginate("users.list", token, {"limit": 200}, "members", cap=20000)
    umap = {}
    for m in members:
        prof = m.get("profile") or {}
        name = prof.get("display_name") or prof.get("real_name") or m.get("real_name") or m.get("name") or m.get("id")
        umap[m.get("id")] = name
    if umap:
        _write_json(USERS_CACHE, {"_ts": time.time(), "map": umap})
    elif cache:
        return cache.get("map", {})
    return umap


_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
_CHANREF = re.compile(r"<#(C[A-Z0-9]+)\|([^>]*)>")
_URLREF = re.compile(r"<(https?://[^|>]+)\|([^>]*)>")


def humanize(text, umap):
    if not text:
        return text
    text = _MENTION.sub(lambda m: "@" + umap.get(m.group(1), m.group(1)), text)
    text = _CHANREF.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = _URLREF.sub(lambda m: m.group(2) or m.group(1), text)
    return text


# ── discovery ──
def discover(token, cfg):
    convs = paginate("users.conversations", token,
                     {"types": "public_channel,private_channel,im,mpim", "exclude_archived": "true", "limit": 200},
                     "channels", cap=2000)
    return convs


def conv_meta(c, umap):
    cid = c.get("id")
    if c.get("is_im"):
        peer = umap.get(c.get("user"), c.get("user"))
        return {"id": cid, "name": "DM:" + str(peer), "is_im": True, "is_mpim": False,
                "is_private": True, "peer": c.get("user")}
    if c.get("is_mpim"):
        return {"id": cid, "name": c.get("name") or "group-dm", "is_im": False, "is_mpim": True, "is_private": True}
    return {"id": cid, "name": c.get("name") or cid, "is_im": False, "is_mpim": False,
            "is_private": bool(c.get("is_private"))}


def ts_iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(KST).isoformat(timespec="seconds")
    except Exception:
        return ""


def permalink(team_url, cid, ts, thread_ts=None):
    if not team_url:
        return ""
    link = "%sarchives/%s/p%s" % (team_url, cid, str(ts).replace(".", ""))
    if thread_ts and thread_ts != ts:
        link += "?thread_ts=%s&cid=%s" % (thread_ts, cid)
    return link


def _msg_text(m):
    """슬랙 메시지 본문 추출 — text 가 비면(노션 봇 등 blocks/attachment 전용) 그쪽에서 뽑는다."""
    t = m.get("text") or ""
    if t.strip():
        return t
    out = []
    for a in (m.get("attachments") or []):
        out.append(a.get("text") or a.get("fallback") or a.get("pretext") or "")
    for b in (m.get("blocks") or []):
        if isinstance(b.get("text"), dict):
            out.append(b["text"].get("text", ""))
        for el in (b.get("elements") or []):
            if isinstance(el, dict):
                for e in (el.get("elements") or []):
                    if isinstance(e, dict) and e.get("text"):
                        out.append(e["text"])
    return " ".join(x for x in out if x).strip()


_ACK_RE = re.compile(
    r"^\s*(?:네+|넵+|넹+|옙+|네넵|알겠습니다|확인했습니다|확인했습니다[.!]*|감사합니다|감사|"
    r"넵\s*감사합니다|네\s*감사합니다|옙\s*알겠습니다|좋습니다|좋아요|오케이|ok|ㅇㅋ|"
    r"ㅋ+|ㅎ+|ㅋㅋ+|ㅎㅎ+|네네|넵넵)[\s!.~]*$",
    re.I,
)
_RESPONSE_RE = re.compile(
    r"(답변|회신|확인\s*부탁|검토\s*부탁|의견\s*부탁|승인|컨펌|결정|"
    r"방향성\s*설정|확정\s*(?:필요|부탁|요청|해)|"
    r"가능(?:할까요|한가요|하실까요|여부|한지|할지)|어떻게|어떡|문의|요청\s*(?:드립니다|드려요|합니다|사항)?|"
    r"\?|？|나요\b|인가요\b|될까요\b|할까요\b|없나\b|주세요|부탁(?:드립니다|드려요)?)",
    re.I,
)
_ACTION_RE = re.compile(
    r"(필요|처리|수정|전달|챙겨|정리|작성|등록|공유\s*부탁|논의(?:해|하|가)?|"
    r"참석|미팅에서|진행\s*부탁|확인해\s*주|봐주)",
    re.I,
)
_FYI_RE = re.compile(r"(cc\.?|참고|공유\s*드|공유드립니다|전달드립니다|안내\s*드|보고\s*드)", re.I)


def _attention_traits(kind, text, kw_hit=None):
    """Return (include, one_shot). one_shot means show once, then auto-dismiss after briefing."""
    clean = re.sub(r"<[@#!][^>]+>", " ", text or "")
    clean = re.sub(r"https?://\S+", " ", clean)
    clean = re.sub(r"[@#][^\s]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    signal = re.sub(r"양해\s*부탁(?:드립니다|드려요)?", " ", clean, flags=re.I)
    signal = re.sub(r"요청\s*건", " ", signal, flags=re.I)
    if not clean:
        return False, False
    if _ACK_RE.match(clean) or (len(clean) <= 12 and re.fullmatch(r"[ㅋㅎ\s!?.~]+", clean)):
        return False, False
    response_required = bool(_RESPONSE_RE.search(signal))
    actionish = response_required or bool(_ACTION_RE.search(signal))
    if kw_hit and not _FYI_RE.search(signal):
        actionish = True
    if not actionish:
        return False, False
    if _FYI_RE.search(signal) and not response_required:
        return True, True
    if kind == "dm":
        return True, not response_required
    if response_required:
        return True, False
    return True, True


def _att_item(kind, m, uname, ts, thread_ts, link, text, keywords, kw_hit=None, one_shot=False):
    kw = kw_hit or next((k for k in keywords if k.lower() in text.lower()), None)
    urgent = bool(kw and any(u.lower() in text.lower() for u in URGENT_KW))
    return {"kind": kind, "urgent": urgent, "channel_id": m["id"], "channel": m["name"],
            "user": uname, "ts": ts, "iso": ts_iso(ts), "thread_ts": thread_ts,
            "permalink": link, "keyword": kw, "one_shot": bool(one_shot),
            "excerpt": (text[:280] + "…") if len(text) > 280 else text}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="refresh conversation discovery only")
    ap.add_argument("--dry-run", action="store_true", help="poll but write nothing")
    args = ap.parse_args()

    os.makedirs(FEED_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    single_instance("slack-fetch")
    cfg = load_config()
    env = load_env(ENV_FILE)
    token = env.get("SLACK_USER_TOKEN", "").strip()
    if not token:
        log("SLACK_USER_TOKEN not set in", ENV_FILE, "→ nothing to do (provision the token to activate).")
        print(json.dumps({"ok": False, "reason": "no_token"}))
        return 0

    auth = slack_call("auth.test", token)
    if not auth.get("ok"):
        log("auth.test failed:", auth.get("error"), "→ check token/scopes.")
        print(json.dumps({"ok": False, "reason": "auth_%s" % auth.get("error")}))
        return 0
    self_id = PROFILE.get("boss_user_id") or auth.get("user_id")
    team_url = auth.get("url") or ""
    self_mention = "<@%s>" % self_id

    umap = load_users(token)
    convs = discover(token, cfg)
    metas = [conv_meta(c, umap) for c in convs]
    # 비서 출력 채널은 폴링/주의/기억 대상에서 제외(자기참조와 비서 간 교차 오염 방지)
    brief_ch = (cfg.get("briefing") or {}).get("private_channel_id", "")
    excl = set(cfg.get("exclude_channels", [])) | assistant_output_channels() | ({brief_ch} if brief_ch else set())
    metas = [m for m in metas if m["id"] not in excl]
    if not args.dry_run:
        _write_json(DISCOVERED_FILE, {"_ts": time.time(), "self_id": self_id, "team_url": team_url,
                                      "channels": metas})
    if args.discover:
        log("discovery refreshed:", len(metas), "conversations")
        print(json.dumps({"ok": True, "discovered": len(metas)}))
        return 0

    key_channels = set(cfg["triage"].get("key_channels", []))
    keywords = [k for k in cfg["triage"].get("keywords", []) if k]
    watch_dms = cfg["triage"].get("watch_dms", True)
    watch_mentions = cfg["triage"].get("watch_mentions", True)
    scope_mode = cfg["memory"].get("ingest_scope", "member")
    min_chars = int(cfg["memory"].get("min_chars", 0))
    max_msgs = int(cfg["poll"].get("max_messages_per_channel", 200))
    max_chans = int(cfg["poll"].get("max_channels", 80))
    lookback = int(cfg["poll"].get("first_run_lookback_hours", 24)) * 3600

    # ingest set
    def in_ingest(m):
        if scope_mode == "key_only":
            return m["is_im"] or m["id"] in key_channels
        return True  # "member"/"all": everything the user belongs to

    # 선택: 핵심채널은 항상 + DM/그룹DM·일반채널은 **클래스별 로테이션**.
    # 대화 수(예: 333)가 캡(80)을 넘으면 고정 우선순위로는 일부 대화가 영원히 안 돌았다
    # (실제 사고: DM 185개가 캡을 다 차지해 일반 채널 멘션을 통째로 놓침).
    # 멘션 감지는 아래 '멘션 스위프'(search API)가 캡과 무관하게 전 워크스페이스를 커버하므로,
    # 폴링은 feed/키워드/DM 수집용으로 전 대화를 순환만 하면 된다.
    cursor = _read_json(CURSOR_FILE, {})
    pool = [m for m in metas if in_ingest(m)]
    dms = [m for m in pool if m["is_im"] or m["is_mpim"]]
    keych = [m for m in pool if not (m["is_im"] or m["is_mpim"]) and m["id"] in key_channels]
    rest = [m for m in pool if not (m["is_im"] or m["is_mpim"]) and m["id"] not in key_channels]
    dm_slice = max(10, int((max_chans - len(keych)) * 0.7))   # DM 쿼터 ~70% (즉답성 우선)
    ch_slice = max(5, max_chans - len(keych) - dm_slice)

    def _rotate(lst, off, n):
        if not lst or n >= len(lst):
            return list(lst)
        off %= len(lst)
        return (lst + lst)[off:off + n]

    rr_dm, rr_ch = int(cursor.get("_rr_dm", 0)), int(cursor.get("_rr_ch", 0))
    ingest = keych + _rotate(dms, rr_dm, dm_slice) + _rotate(rest, rr_ch, ch_slice)
    cursor["_rr_dm"] = (rr_dm + dm_slice) % max(1, len(dms))
    cursor["_rr_ch"] = (rr_ch + ch_slice) % max(1, len(rest))
    now = time.time()
    new_msgs = 0
    att = _read_json(ATTENTION_FILE, {}).get("open", {})  # keyed: 'dm:<cid>' / 'mention:<cid>:<ts>' / 'kw:<cid>:<ts>'
    new_keys = []  # 이번 사이클에 새로 생긴 주의 키(알림 트리거용)
    feed_lines = {}  # cid -> [json line, ...]

    for m in ingest:
        cid = m["id"]
        oldest = cursor.get(cid)
        if not oldest:
            oldest = "%.6f" % (now - lookback)
        msgs = paginate("conversations.history", token,
                        {"channel": cid, "oldest": oldest, "limit": 200}, "messages", cap=max_msgs)
        if not msgs:
            continue
        msgs = sorted(msgs, key=lambda x: float(x.get("ts", "0")))
        max_ts = oldest
        for msg in msgs:
            ts = msg.get("ts")
            if not ts or float(ts) <= float(oldest):
                continue
            if msg.get("subtype") in ("channel_join", "channel_leave", "channel_topic", "channel_purpose"):
                max_ts = ts
                continue
            raw = _msg_text(msg)
            text = humanize(redact(raw), umap)
            if not text.strip() or len(text.strip()) < min_chars:
                max_ts = ts
                continue
            uid = msg.get("user") or msg.get("bot_id") or ""
            uname = umap.get(uid, uid)
            thread_ts = msg.get("thread_ts")
            link = permalink(team_url, cid, ts, thread_ts)
            rec = {"v": 1, "kind": "slack_msg", "host": "slack", "scope": "company",
                   "channel_id": cid, "channel": m["name"], "is_dm": m["is_im"], "is_mpim": m["is_mpim"],
                   "user_id": uid, "user": uname, "ts": ts, "iso": ts_iso(ts),
                   "thread_ts": thread_ts, "permalink": link, "text": text}
            feed_lines.setdefault(cid, []).append(json.dumps(rec, ensure_ascii=False))
            new_msgs += 1
            max_ts = ts

            # 주의 큐(키 기반). 내가(self) 마지막에 답하면 자동 해결되어 다음 사이클부터 안 뜬다.
            is_self = (uid == self_id)
            # 봇 발신(Notion·Google Calendar·Slackbot 알림 등)은 '답할 DM'이 아니다 — feed/기억엔 남기되 주의큐 제외
            is_bot_msg = (bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message"
                          or uid.startswith("B") or uid == "USLACKBOT")
            if m["is_im"]:
                k = "dm:" + cid
                if is_self:
                    att.pop(k, None)                       # 내가 답함 → 해결
                elif watch_dms and not is_bot_msg:
                    include, one_shot = _attention_traits("dm", text)
                    if include:
                        if k not in att:
                            new_keys.append(k)
                        att[k] = _att_item("dm", m, uname, ts, thread_ts, link, text, keywords,
                                           one_shot=one_shot)  # DM당 미답 1건
            else:
                if is_self:
                    # 내가 이 채널에 글을 씀 → 그 시점 이전의 멘션/키워드 항목 해결
                    for kk in [x for x in att if x.startswith("mention:" + cid + ":") or x.startswith("kw:" + cid + ":")]:
                        if float(kk.rsplit(":", 1)[1]) <= float(ts):
                            att.pop(kk, None)
                elif watch_mentions and (self_mention in raw):
                    k = "mention:%s:%s" % (cid, ts)
                    include, one_shot = _attention_traits("mention", text)
                    if include:
                        if k not in att:
                            new_keys.append(k)
                        att[k] = _att_item("mention", m, uname, ts, thread_ts, link, text, keywords,
                                           one_shot=one_shot)
                elif cid in key_channels:
                    kw_hit = next((kw for kw in keywords if kw.lower() in text.lower()), None)
                    if kw_hit:
                        k = "kw:%s:%s" % (cid, ts)
                        include, one_shot = _attention_traits("keyword", text, kw_hit)
                        if include:
                            if k not in att:
                                new_keys.append(k)
                            att[k] = _att_item("keyword", m, uname, ts, thread_ts, link, text, keywords,
                                               kw_hit, one_shot=one_shot)
        cursor[cid] = max_ts

    # ── 멘션 스위프: 검색 API 1콜로 전 워크스페이스의 나 멘션을 잡는다(폴링 캡·스레드와 무관).
    #    폴링 기반 감지는 보조로 남고, 캡 때문에 멘션을 놓치는 구조적 구멍을 여기서 막는다.
    if watch_mentions:
        last_sweep = float(cursor.get("_mention_sweep_ts", now - lookback))
        sr = slack_call("search.messages", token,
                        {"query": self_mention, "sort": "timestamp", "sort_dir": "desc", "count": 50})
        if sr.get("ok"):
            meta_by_id = {m["id"]: m for m in metas}
            max_seen = last_sweep
            for match in (sr.get("messages") or {}).get("matches", []):
                ts = match.get("ts") or "0"
                cid = (match.get("channel") or {}).get("id") or ""
                max_seen = max(max_seen, float(ts))
                muid = match.get("user") or ""
                if float(ts) <= last_sweep or not cid or cid in excl or muid == self_id:
                    continue
                mm = meta_by_id.get(cid) or {"id": cid, "name": (match.get("channel") or {}).get("name") or cid,
                                             "is_im": False, "is_mpim": False}
                pl = match.get("permalink") or permalink(team_url, cid, ts)
                th = re.search(r"thread_ts=([0-9.]+)", pl)
                thread_ts = th.group(1) if th else None
                text = humanize(redact(_msg_text(match)), umap)
                if not text.strip():   # blocks 전용인데 검색결과에 본문이 없으면 빈 항목 — 큐 제외
                    continue
                uname = umap.get(muid, match.get("username") or muid)
                k = "mention:%s:%s" % (cid, ts)
                include, one_shot = _attention_traits("mention", text)
                if include:
                    if k not in att:
                        new_keys.append(k)
                    att[k] = _att_item("mention", mm, uname, ts, thread_ts, pl, text, keywords,
                                       one_shot=one_shot)
            cursor["_mention_sweep_ts"] = "%.6f" % max_seen
        else:
            log("mention sweep failed:", sr.get("error"))

    # ── 열린 멘션 자동 해결: 그 스레드/채널에 내가 멘션 이후 답했는지 직접 확인(로테이션 대기 없이 즉시).
    for k in sorted([x for x in att if x.startswith("mention:")],
                    key=lambda x: -float(x.rsplit(":", 1)[1]))[:10]:
        it = att[k]
        cid, mts, th = it.get("channel_id"), it.get("ts"), it.get("thread_ts")
        method = "conversations.replies" if th else "conversations.history"
        params = {"channel": cid, "oldest": mts, "limit": 20}
        if th:
            params["ts"] = th
        res = slack_call(method, token, params)
        msgs2 = res.get("messages") or [] if res.get("ok") else []
        if any(mm2.get("user") == self_id and float(mm2.get("ts", "0")) > float(mts) for mm2 in msgs2):
            att.pop(k, None)
            if k in new_keys:
                new_keys.remove(k)

    if args.dry_run:
        log("dry-run:", new_msgs, "new msgs,", len(new_keys), "new attention,", len(att), "open (not written)")
        print(json.dumps({"ok": True, "dry_run": True, "new_messages": new_msgs,
                          "attention_new": len(new_keys), "attention_open": len(att)}, ensure_ascii=False))
        return 0

    # append feed lines per channel
    for cid, lines in feed_lines.items():
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", cid)
        fp = os.path.join(FEED_DIR, safe + ".jsonl")
        with open(fp, "a") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(fp, 0o600)

    # prune: 가장 최근 200건만 유지(키 기반)
    if len(att) > 200:
        for k in sorted(att, key=lambda x: float(att[x].get("ts", "0")))[:-200]:
            att.pop(k, None)
    _write_json(ATTENTION_FILE, {"_ts": time.time(), "self_id": self_id, "team_url": team_url, "open": att})
    _write_json(CURSOR_FILE, cursor)

    summary = {"ok": True, "channels_polled": len(ingest), "new_messages": new_msgs,
               "attention_new": len(new_keys), "attention_open": len(att),
               "ts": datetime.now(KST).isoformat(timespec="seconds")}
    log("cycle:", json.dumps(summary, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
