#!/usr/bin/env python3
"""
tory_command_watcher.py — 비서 채널(#승현-비서 C0B997W7KGS) 즉시 명령 수신기.

오승현(U03EQFWTD61)이 비서 채널에 메시지를 올리면 수 초 내(폴링 7s) 감지해
headless Claude(CLIProxyAPI :8321 경유 — 구독 프록시, 유료 API 아님)로 수행하고,
결과를 토리 봇(bot token)으로 해당 메시지의 스레드에 회신한다.

구조적 발송 게이트:
  - headless Claude 에는 발송 도구가 전혀 없다(allowedTools = 읽기 도구 + Slack
    읽기 전용 헬퍼 + 메모리 검색만). 회신 게시는 이 워처가 비서 채널 스레드에만 한다.
  - Gmail/Notion 등 데스크톱 커넥터가 필요한 부분은 Claude 가 [[HANDOFF]] 블록으로
    표시 → ~/.torymemory/deep-briefs/handoff-queue.json 에 적재 → 30분 주기
    tory-command-listener(Claude 앱 스케줄 작업)가 마저 처리한다.

launchd: com.tory.tory-command-watcher (KeepAlive, 내부 루프).
수동 디버그: tory_command_watcher.py --once   (1회 폴링 후 종료)
stdlib only. 모든 시각 KST 고정.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    from datetime import timedelta, timezone
    KST = timezone(timedelta(hours=9), "KST")

# 공용 가독성 렌더·규칙·노이즈 판정(브리핑·문의응답·조사보고 통일). 모듈 부재/오류 시 기존 동작으로 폴백.
try:
    from tory_format import render_slack, READ_CORE, is_noise, clean_excerpt
except Exception:
    READ_CORE = ""
    def render_slack(t):
        return re.sub(r"\*\*(.+?)\*\*", r"*\1*", t or "")
    def is_noise(t):
        return not (t or "").strip()
    def clean_excerpt(t):
        return (t or "").replace("\n", " ").strip()

# 답변 가독성: 텍스트 → Block Kit(헤더/섹션/구분선/컨텍스트). 실패 시 text 폴백.
try:
    from tory_blocks import to_blocks
except Exception:
    def to_blocks(text, footer=None):
        return [], (text or "")

HOME = os.path.expanduser("~")
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    assistant_config = None
    PROFILE = {}

BASE = PROFILE.get("base_dir") or os.path.join(HOME, ".torymemory")
ENV_FILE = PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env")
STATE_DIR = PROFILE.get("state_dir") or os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "command-watcher-state.json")
SLACK_ATTN = os.path.join(STATE_DIR, "slack-attention.json")
GOOGLE_ATTN = os.path.join(STATE_DIR, "google-attention.json")
BRIEF_STATE = os.path.join(STATE_DIR, "slack-brief-state.json")
DEEP_BRIEFS_DIR = PROFILE.get("deep_briefs_dir") or os.path.join(BASE, "deep-briefs")
HANDOFF_FILE = os.path.join(DEEP_BRIEFS_DIR, "handoff-queue.json")
TRACKING_BOARD = os.path.join(DEEP_BRIEFS_DIR, "tracking-board.json")
WORK_HOURS_STATE = os.path.join(STATE_DIR, "work-hours.json")
CREDIT_REQUEST = os.path.join(STATE_DIR, "credit-request.json")  # 호스트 크레딧 잡(tory_credit.py tick)이 폴링
OUTBOX_DIR = PROFILE.get("outbox_dir") or os.path.join(BASE, "outbox")
OUTBOX_FAILED = os.path.join(OUTBOX_DIR, "failed")
WORKDIR = PROFILE.get("workdir") or os.path.join(BASE, "claude-workdir")
SLACK_API = "https://slack.com/api/"
IS_DEFAULT_PROFILE = (PROFILE.get("id") or "tory") == "tory"
CHANNEL = PROFILE.get("assistant_channel_id") or ("C0B997W7KGS" if IS_DEFAULT_PROFILE else "")
BOSS = PROFILE.get("boss_user_id") or ("U03EQFWTD61" if IS_DEFAULT_PROFILE else "")
ASSISTANT_NAME = PROFILE.get("assistant_name") or PROFILE.get("slack_username") or "토리"
ASSISTANT_ICON = PROFILE.get("slack_icon_emoji") or ":card_index_dividers:"
BOSS_NAME = PROFILE.get("boss_name") or "오승현"
BOSS_TITLE = PROFILE.get("boss_title") or "전략본부장"
COMPANY_NAME = PROFILE.get("company_name") or "ASWEMAKE"
CHANNEL_NAME = PROFILE.get("assistant_channel_name") or "승현-비서"
ENABLED_SOURCES = set((PROFILE.get("enabled_sources") or
                       ["slack", "gmail", "calendar", "drive", "notion", "memory", "local", "recordings"]))
ENABLED_ACTIONS = set((PROFILE.get("enabled_actions") or ["slack", "gmail", "calendar", "notion"]))
_SOURCE_LABELS = {
    "slack": "Slack",
    "gmail": "Gmail",
    "calendar": "Calendar",
    "drive": "Drive",
    "notion": "Notion",
    "memory": "회사 메모리",
    "local": "로컬 상태",
    "recordings": "회의록",
}
SOURCE_LABEL = "·".join(_SOURCE_LABELS.get(s, s) for s in
                         ["slack", "gmail", "calendar", "drive", "notion", "memory", "local", "recordings"]
                         if s in ENABLED_SOURCES) or "설정된 소스 없음"
CLAUDE_BIN = os.path.join(HOME, ".local", "bin", "claude")
ANTHROPIC_BASE = "http://localhost:8321"
POLL_SEC = 7
CLAUDE_TIMEOUT = 540
MAX_REPLY = 11500
BACKLOG_CAP_SEC = 1800  # 다운타임 후 30분 이전 메시지는 재생하지 않는다(폭주 방지)
LOG = "[tory-watcher]"

TRIVIAL = {"ㅇㅋ", "ㅋㅋ", "ㅎㅎ", "넵", "네", "넹", "예", "응", "오케이", "ok", "오키",
           "감사", "감사합니다", "고마워", "고맙습니다", "좋아", "좋아요", "확인", "완료",
           "ㅇㅇ", "됐어", "됐다", "굿", "👍"}
TODO_RE = re.compile(r"(나\s*)?(뭐|무엇|머|모)\s*(해야|할까|하지|하면)|할\s*일|우선\s*순위|지금\s*(뭐|무엇|머|모)", re.I)
OFFWORK_RE = re.compile(r"^\s*(퇴근\s*(했다|했어|했음|함|합니다|할게|완료)?|오늘\s*(끝|마감)|업무\s*(종료|마감))\s*[.!~]*\s*$", re.I)
ONWORK_RE = re.compile(r"^\s*(출근\s*(했다|했어|했음|함|합니다|완료)?|업무\s*(시작|재개))\s*[.!~]*\s*$", re.I)
TOOL_LEAK_RE = re.compile(r"\bto\s*=\s*(slack_read|gmail_read|notion_read|drive_read|memory_read|local_read)\b")
META_LEAK_RE = re.compile(
    r"(The test worked|body parameter was the issue|Let me create the actual report|"
    r"I should not submit another|Since I already used|Actually,?\s+looking at the error|"
    r"test proposal went through|provide the full report in my response|"
    r"what will be posted as a thread reply)",
    re.I,
)
EMPLOYEE_HINT_RE = re.compile(r"(직원|사람|누구|누군|프로필|소속|부서|직함|담당|역할|연락|전화|메일|이메일|찾아|찾아줘|알려|유저|슬랙|노션|입사|퇴사|어디)", re.I)
EMPLOYEE_TASK_RE = re.compile(r"(해야|할\s*일|todo|투두|담당|역할|업무|프로젝트|마감|요청|부탁|확인|진행|공유|피드백|초안|기획|개발|온보딩|픽스|리뷰|작업)", re.I)
EMPLOYEE_STOPWORDS = {
    "직원", "사람", "누구", "누군", "프로필", "소속", "부서", "직함", "담당", "역할", "연락",
    "전화", "메일", "이메일", "찾아", "찾아줘", "알려", "유저", "슬랙", "노션", "입사", "퇴사",
    "어디", "뭐해", "뭐함", "뭐야", "누구야", "알려줘", "해줘", "주세요", "확인", "검색",
    "사업부", "본부", "전략", "회의록", "경영진", "내용", "전부", "관련", "방안", "건에",
}
COMMON_SURNAMES = set("김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민류진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국")
BARE_COMMAND_SUFFIXES = ("줘", "해", "하자", "하라", "봐", "보자", "완료", "확인")
CONTACT_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+|0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}")

TOOL_BIN = os.environ.get("TORY_ASSISTANT_BIN") or os.path.join(BASE, "bin")

ALLOWED_TOOLS = ",".join([
    "ToolSearch", "Read", "Glob", "Grep",
    "Bash(%s/tory_slack_read.sh:*)" % TOOL_BIN,
    "Bash(%s/tory_gmail_read.py:*)" % TOOL_BIN,
    "Bash(%s/tory_notion_read.sh:*)" % TOOL_BIN,
    "Bash(%s/tory_drive_read.py:*)" % TOOL_BIN,
    "mcp__openmemory__search_memory", "mcp__openmemory__list_memories",
    "mcp__awm-confidential__search_memory", "mcp__awm-confidential__list_memories",
    "mcp__company-memory__search_company_memory", "mcp__company-memory__list_company_memories",
    "mcp__company-memory__get_company_memory",
])
DISALLOWED_TOOLS = ",".join([
    "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task",
    "mcp__openmemory__add_memories", "mcp__awm-confidential__add_memories",
    "mcp__company-memory__add_memories", "mcp__company-memory__add_company_memory",
    "mcp__company-memory__add_memries",
])


def log(*a):
    print(LOG, datetime.now(KST).strftime("%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _next_start_8():
    now = datetime.now(KST)
    start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= start:
        start += timedelta(days=1)
    return start


def _is_offwork_request(text):
    return bool(OFFWORK_RE.search(text or ""))


def _is_onwork_request(text):
    return bool(ONWORK_RE.search(text or ""))


# 크레딧/비용 즉답 게이트 — 비서 채널에서 'ocr 크레딧/비용/사용량 조회·알려줘' 류 짧은 명령.
# 컨테이너(헤드리스)는 콘솔 Chrome 을 못 띄우므로 여기선 요청만 기록(credit-request.json)하고,
# 호스트 잡(tory_credit.py tick, launchd)이 로그인된 전용 프로필로 긁어 outbox 로 회신한다.
CREDIT_NOUN_RE = re.compile(r"(크레[디딧]트?|크래딧|credit|비용|cost|사용\s*량|usage|토큰)", re.I)
CREDIT_VERB_RE = re.compile(r"(조회|알려|보여|얼마|현황|리포트|report|뽑아|확인해|어때)", re.I)


def _is_credit_request(text):
    t = (text or "").strip()
    if len(t) > 30:
        return False
    return bool(CREDIT_NOUN_RE.search(t) and CREDIT_VERB_RE.search(t))


def _request_credit(thread_ts, src_ts, kind="both"):
    payload = {"channel": CHANNEL, "thread_ts": thread_ts, "src_ts": src_ts,
               "kind": kind, "requested_at": datetime.now(KST).isoformat(timespec="seconds")}
    try:
        tmp = CREDIT_REQUEST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, CREDIT_REQUEST)
        return True
    except Exception as e:
        log("credit request write failed:", str(e)[:120])
        return False


def _set_offwork():
    resume = _next_start_8()
    state = {
        "offwork": True,
        "offwork_at": datetime.now(KST).isoformat(timespec="seconds"),
        "resume_at": resume.timestamp(),
        "resume_iso": resume.isoformat(timespec="seconds"),
        "resume_rule": "daily 08:00 KST",
    }
    _write_json(WORK_HOURS_STATE, state)
    return state


def _set_onwork():
    state = {
        "offwork": False,
        "onwork_at": datetime.now(KST).isoformat(timespec="seconds"),
        "resume_rule": "manual",
    }
    _write_json(WORK_HOURS_STATE, state)
    return state


def _offwork_active():
    st = _read_json(WORK_HOURS_STATE, {})
    return bool(st.get("offwork")) and time.time() < _f(st.get("resume_at"), 0)


def _run_read(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return json.dumps({"ok": False, "error": "exec:%s" % str(e)[:120]}, ensure_ascii=False)


def _loads(s):
    try:
        return json.loads(s)
    except Exception:
        return {}


def _redact_contacts(text):
    return CONTACT_RE.sub("[연락처 비공개]", text or "")


def _clean_employee_name(name):
    name = name or ""
    if len(name) >= 3 and name[-1] in "은는이가":
        name = name[:-1]
    name = re.sub(r"(님|씨)$", "", name)
    return name


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _item_key(a):
    if a.get("kind") == "dm":
        return "dm:" + str(a.get("channel_id"))
    return "%s:%s:%s" % (a.get("kind"), a.get("channel_id"), a.get("ts"))


def _is_todo_request(text):
    return bool(TODO_RE.search(text or ""))


def _extract_employee_query(text):
    t = re.sub(r"<@[^>]+>", " ", text or "").strip()
    # 작성/정리/조사 등 '작업 요청'이면 직원 조회가 아니다 → None(조사 경로로 보낸다).
    #  'DX사업부 … 작성해줘' 가 '사업부' 직원 조회로 빠지던 회귀 차단(2026-06-15).
    if re.search(r"(작성|정리|취합|종합|분석|만들|보고|초안|검토|승인|요청|기획|살펴|올려|넣어|준비)", t):
        return None
    m = re.search(r"([가-힣]{2,4})(?:님|씨)?(?:은|는|이|가)?\s*(뭐|무엇|무슨|어떤)?\s*(해야|할\s*일|담당|역할|업무|프로젝트|소속|부서|누구)", t)
    if m:
        name = _clean_employee_name(m.group(1))
        if len(name) >= 2 and name not in EMPLOYEE_STOPWORDS:
            return name
    bare = re.sub(r"\s+", "", t)
    if re.fullmatch(r"[가-힣]{2,4}(님|씨)?", bare):
        bare_name = _clean_employee_name(bare)
        if len(bare_name) >= 3 and bare_name[0] in COMMON_SURNAMES and not bare_name.endswith(BARE_COMMAND_SUFFIXES):
            return bare_name
    if not EMPLOYEE_HINT_RE.search(t):
        return None
    for c in re.findall(r"[가-힣]{2,5}", t):
        c = _clean_employee_name(c)
        if len(c) < 2 or c in EMPLOYEE_STOPWORDS:
            continue
        if any(c.startswith(sw) or c.endswith(sw) for sw in EMPLOYEE_STOPWORDS):
            continue
        return c
    return None


def _slack_user_label(member):
    p = member.get("profile") or {}
    real = p.get("real_name") or member.get("real_name") or ""
    display = p.get("display_name") or member.get("name") or ""
    title = _redact_contacts(p.get("title") or "")
    parts = [real or display]
    if display and display != real:
        parts.append("(%s)" % display)
    if title:
        parts.append("— %s" % title)
    parts.append("`%s`" % (member.get("id") or "?"))
    return " ".join(p for p in parts if p)


def _slack_profile_matches(name, limit=5):
    out = _run_read(["bash", os.path.join(BASE, "bin", "tory_slack_read.sh"), "users.list", "limit=500"], timeout=30)
    data = _loads(out)
    members = data.get("members") or []
    q = re.sub(r"\s+", "", name or "").lower()
    rows = []
    for m in members:
        if m.get("deleted") or m.get("is_bot"):
            continue
        p = m.get("profile") or {}
        fields = [m.get("name"), m.get("real_name"), p.get("real_name"), p.get("display_name")]
        hay = " ".join(x or "" for x in fields)
        norm = re.sub(r"\s+", "", hay).lower()
        if q and q in norm:
            exact = any(q == re.sub(r"\s+", "", x or "").lower() for x in fields)
            rows.append((0 if exact else 1, _slack_user_label(m)))
    rows.sort(key=lambda x: x[0])
    return [r for _, r in rows[:limit]]


def _slack_message_matches(name, limit=4):
    q = urllib.parse.urlencode({"query": '"%s"' % name, "sort": "timestamp", "sort_dir": "desc", "count": str(limit)})
    out = _run_read(["bash", os.path.join(BASE, "bin", "tory_slack_read.sh"), "search.messages", q], timeout=30)
    data = _loads(out)
    matches = ((data.get("messages") or {}).get("matches") or []) if isinstance(data, dict) else []
    rows = []
    for m in matches[:limit]:
        ch = m.get("channel") or {}
        channel = ch.get("name") or ch.get("id") or "?"
        if ch.get("id") == CHANNEL or channel == CHANNEL_NAME:
            continue
        user = m.get("user_name") or m.get("username") or m.get("user") or "?"
        text = re.sub(r"\s+", " ", _redact_contacts(m.get("text") or m.get("previous", {}).get("text") or "")).strip()
        if TOOL_LEAK_RE.search(text):
            continue
        link = m.get("permalink") or ""
        rows.append("#%s · %s — %s%s" % (channel, user, text[:180], (" <%s|열기>" % link) if link else ""))
    return rows


def _slack_employee_task_rows(name, limit=5):
    q = urllib.parse.urlencode({"query": '"%s"' % name, "sort": "timestamp", "sort_dir": "desc", "count": "20"})
    out = _run_read(["bash", os.path.join(BASE, "bin", "tory_slack_read.sh"), "search.messages", q], timeout=30)
    data = _loads(out)
    matches = ((data.get("messages") or {}).get("matches") or []) if isinstance(data, dict) else []
    rows = []
    fallback = []
    for m in matches:
        ch = m.get("channel") or {}
        channel = ch.get("name") or ch.get("id") or "?"
        if ch.get("id") == CHANNEL or channel == CHANNEL_NAME:
            continue
        user = m.get("user_name") or m.get("username") or m.get("user") or "?"
        text = re.sub(r"\s+", " ", _redact_contacts(m.get("text") or m.get("previous", {}).get("text") or "")).strip()
        if not text or TOOL_LEAK_RE.search(text):
            continue
        link = m.get("permalink") or ""
        row = "#%s · %s — %s%s" % (channel, user, text[:210], (" <%s|열기>" % link) if link else "")
        if EMPLOYEE_TASK_RE.search(text):
            rows.append(row)
        else:
            fallback.append(row)
        if len(rows) >= limit:
            break
    return (rows or fallback)[:limit]


def _notion_title(row):
    props = row.get("properties") or {}
    for p in props.values():
        if isinstance(p, dict) and p.get("type") == "title":
            return "".join(x.get("plain_text") or "" for x in (p.get("title") or [])).strip()
    for k in ("title", "name", "Name"):
        v = row.get(k) or props.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("title"):
            return "".join(x.get("plain_text") or "" for x in v.get("title") or []).strip()
    return row.get("id") or "(제목 없음)"


def _notion_matches(name, limit=4):
    out = _run_read(["bash", os.path.join(BASE, "bin", "tory_notion_read.sh"), "search", name], timeout=30)
    data = _loads(out)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raw = re.sub(r"\s+", " ", _redact_contacts(out)).strip()
        return [raw[:220]] if raw else []
    rows = []
    for r in results[:limit]:
        title = _redact_contacts(_notion_title(r))
        url = r.get("url") or ""
        rows.append("%s%s" % (title[:180], (" <%s|열기>" % url) if url else ""))
    return rows


def build_employee_answer(name):
    profiles = _slack_profile_matches(name)
    task_rows = _slack_employee_task_rows(name)
    slack_rows = _slack_message_matches(name)
    notion_rows = _notion_matches(name)
    lines = ["*%s* 관련 todo를 Slack + Notion 기준으로만 봤습니다." % name]
    if task_rows:
        lines.append("*할 일/업무 후보*")
        lines.extend("- " + x for x in task_rows)
    else:
        lines.append("*할 일/업무 후보*\n- Slack 최근 언급에서 뚜렷한 todo 후보를 못 찾았습니다.")
    if profiles:
        lines.append("*프로필 참고*")
        lines.extend("- " + x for x in profiles)
    else:
        lines.append("*프로필 참고*\n- 정확히 일치하는 활성 Slack 사용자를 못 찾았습니다.")
    if slack_rows and not task_rows:
        lines.append("*최근 Slack 근거*")
        lines.extend("- " + x for x in slack_rows[:3])
    if notion_rows:
        lines.append("*관련 Notion*")
        lines.extend("- " + x for x in notion_rows)
    if not task_rows and not slack_rows and not notion_rows:
        lines.append("최근 Slack 메시지/Notion 문서에서 추가 근거는 못 찾았습니다.")
    lines.append("이 직원 조회는 당신이 사람을 직접 물었을 때만 실행합니다. Gmail/Drive/Memory/local은 이 경로에서 보지 않습니다.")
    return "\n".join(lines)[:MAX_REPLY]


def _todo_items():
    """현재 처리할 일 스냅샷. LLM 없이도 답할 수 있게 state JSON만 사용."""
    slack = _read_json(SLACK_ATTN, {})
    google = _read_json(GOOGLE_ATTN, {})
    brief = _read_json(BRIEF_STATE, {})
    dismissed = brief.get("dismissed") or {}
    items = list((slack.get("open") or {}).values()) + list(google.get("items") or [])
    out = []
    for a in items:
        ex = clean_excerpt(a.get("excerpt") or a.get("text") or "")
        if not ex:
            continue
        if a.get("kind") == "dm" and len(ex) < 20 and not any(x in ex for x in ("?", "？", "확인", "부탁", "요청", "필요", "가능", "해줘", "주세요")):
            continue
        k = _item_key(a)
        if k in dismissed and _f(a.get("ts")) <= _f(dismissed.get(k)):
            continue
        b = dict(a)
        b["_key"] = k
        b["_excerpt"] = ex
        out.append(b)

    brief_order = {k: i for i, k in enumerate(brief.get("item_keys") or brief.get("briefed_keys") or [])}
    kind_order = {"mention": 0, "email": 1, "dm": 2, "keyword": 3}
    out.sort(key=lambda a: (
        brief_order.get(a["_key"], 999),
        0 if a.get("urgent") else 1,
        kind_order.get(a.get("kind"), 9),
        -_f(a.get("ts")),
    ))
    return out


def _tracking_items(limit=3):
    board = _read_json(TRACKING_BOARD, {})
    rows = []
    for it in board.get("items") or []:
        st = (it.get("status") or "").lower()
        if st in {"done", "closed", "skipped"}:
            continue
        rows.append(it)
    pri = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows.sort(key=lambda x: (pri.get(x.get("priority"), 9), x.get("created") or ""))
    return rows[:limit]


def build_todo_snapshot(limit=6):
    rows = []
    now = time.time()
    for i, a in enumerate(_todo_items()[:limit], 1):
        age_h = max(0.0, (now - _f(a.get("ts"))) / 3600.0)
        title = a.get("channel") or a.get("kind") or "?"
        who = a.get("user") or "?"
        ex = a.get("_excerpt", "")[:180]
        link = a.get("permalink") or ""
        rows.append("%d. #%s · %s · %.1fh 대기 — %s%s" % (
            i, title, who, age_h, ex, (" <%s|열기>" % link) if link else ""))
    tracks = _tracking_items()
    if tracks:
        rows.append("추적보드 open: " + " / ".join(
            "%s %s" % (t.get("priority") or "P?", (t.get("title") or t.get("id") or "")[:60])
            for t in tracks))
    return "\n".join(rows) if rows else "(현재 attention/google/tracking 기준 즉시 처리할 항목 없음)"


def build_todo_answer(limit=5):
    items = _todo_items()[:limit]
    if not items:
        return "지금 큐 기준으로 바로 처리할 항목은 없습니다. 새 멘션·메일이 들어오면 다시 띄울게요."
    lines = ["지금은 이 순서로 처리하세요."]
    for i, a in enumerate(items, 1):
        ex = a.get("_excerpt", "")
        link = a.get("permalink") or ""
        if a.get("kind") == "email":
            action = "메일 확인/처리"
        elif a.get("kind") == "dm":
            action = "DM 답장 여부 확인"
        else:
            action = "스레드 답변"
        lines.append("%d. *%s* — #%s · %s\n   %s%s" % (
            i, action, a.get("channel") or "?", a.get("user") or "?",
            ex[:220], ("\n   <%s|열기>" % link) if link else ""))
    tracks = _tracking_items(2)
    if tracks:
        lines.append("추적보드도 열려 있습니다: " + " / ".join(
            "%s %s" % (t.get("priority") or "P?", t.get("title") or t.get("id") or "")
            for t in tracks))
    # 노션 task 캐시는 더 이상 todo 답변에 포함하지 않는다(2026-06-22 사용자 지시).
    return "\n".join(lines)


_LOCK_FH = None


def single_instance(name):
    """launchd KeepAlive 재기동과 수동 실행이 겹치는 것을 차단."""
    global _LOCK_FH
    os.makedirs(STATE_DIR, exist_ok=True)
    _LOCK_FH = open(os.path.join(STATE_DIR, name + ".lock"), "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another instance is running — exit")
        sys.exit(0)


def slack_call(method, token, params=None, post=False, timeout=15):
    params = dict(params or {})
    headers = {"Authorization": "Bearer " + token}
    if post:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(SLACK_API + method, data=data, headers=headers)
    else:
        req = urllib.request.Request(SLACK_API + method + "?" + urllib.parse.urlencode(params), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"ok": False, "error": "http:%s" % e}


def kst_hm(ts):
    try:
        return datetime.fromtimestamp(float(ts), KST).strftime("%H:%M")
    except Exception:
        return "?"


def is_request(m):
    """보스의 톱레벨 일반 메시지만 — 봇·서브타입·스레드답글·완료표시성 한마디 제외.
    노이즈 판정(웃음·이모지·인정·잡담)은 tory_format.is_noise 로 단일화(ㅋㅋㅋ→'' 누락 버그 수정)."""
    if m.get("user") != BOSS or m.get("subtype") or m.get("bot_id"):
        return False
    if m.get("thread_ts") and m.get("thread_ts") != m.get("ts"):
        return False
    text = (m.get("text") or "").strip()
    if not text or is_noise(text):
        return False
    return True


def build_context(messages, req_ts, root_ts=None):
    """요청 직전 채널/스레드 메시지 몇 개를 맥락으로. 스레드 후속이면 루트(브리핑)는
    번호↔항목 매핑이 살도록 길게 남긴다 — '3번' 같은 지정이 풀리게."""
    ordered = sorted(messages, key=lambda x: float(x.get("ts", 0)))
    root_line = None
    lines = []
    for idx, m in enumerate(ordered):
        if m.get("ts") == req_ts:
            continue
        text = clean_excerpt(m.get("text") or "")
        if not text:
            continue
        who = BOSS_NAME if m.get("user") == BOSS else (ASSISTANT_NAME + "브리핑" if m.get("bot_id") else (m.get("user") or "?"))
        is_root = (root_ts and m.get("ts") == root_ts) or (root_ts is None and idx == 0)
        cap = 1400 if is_root else 240
        line = "[%s] %s: %s" % (kst_hm(m.get("ts")), who, text[:cap])
        if root_ts and m.get("ts") == root_ts:
            root_line = line   # 루트는 잘려나가지 않게 항상 선두 고정
        else:
            lines.append(line)
    tail = lines[-7:] if root_line else lines[-8:]
    out = ([root_line] if root_line else []) + tail
    return "\n".join(out) or "(직전 맥락 없음)"


PROMPT_TMPL = f"""너는 "{ASSISTANT_NAME}" — {COMPANY_NAME} {BOSS_NAME} {BOSS_TITLE}의 개인 비서다. {BOSS_NAME}이 슬랙 비서 채널(#{CHANNEL_NAME})에 남긴 아래 요청을 지금 수행하라. 너의 최종 출력 텍스트가 그대로 그 메시지의 스레드 답글로 게시된다(다른 곳으로는 절대 나가지 않는다).

[요청] {BOSS_NAME}, %(when)s KST
%(request)s

[직전 채널 맥락 — 참고용]
%(context)s

[현재 할 일 스냅샷 — "나 뭐해야 돼/할 일/우선순위" 질문이면 이것을 최우선 근거로 답하라]
%(todo_context)s

[일하는 방식 — 반드시 지켜라]
- 표면 문장이 아니라 "진짜 의도·맥락·원하는 결과"를 먼저 파악하라.
- **자료의 시점을 확인하라(중요).** 문서·PPT·회의록은 공유일/수정일을 보고, 같은 주제 여러 버전이면 *가장 최신만* 근거로 삼아라(예: '202603'보다 '202605'). 오래된 자료는 '(YYYY-MM 기준 — 현재와 다를 수 있음)'으로 명시하고 현재 사실로 단정하지 마라. 최근 Slack 활동·노션 최신 편집과 충돌하면 최신을 따르고, 확정 안 되면 '최신 확인 필요'로 남겨라.
- **자료에 없는 내용을 지어내지 마라(절대).** 초안·보고에 넣는 모든 항목·역할·R&R·수치·일정은 네가 *실제로 읽은 자료에 명시적으로 있어야* 한다. 과거 관행·일반론·추정·'그럴듯함'으로 채우지 마라. 특히 사람의 직책/역할은 자료에 적힌 그대로만(예: PPT에 '리더 김규환'이면 거기까지만 — '사업부장 보좌·표준 검토자' 같은 미기재 역할 창작 금지). 근거 없는 줄은 빼거나 끝에 '(자료 미확인)'으로 표시하라. 한 항목이라도 지어내면 전체 신뢰가 깨진다.
- "나 뭐해야 돼?", "지금 뭐 하지?", "할 일 알려줘" 같은 질문은 직전 스레드 맥락이 없어도 위 현재 할 일 스냅샷과 필요시 local_read(state/slack-attention.json, state/google-attention.json, deep-briefs/tracking-board.json)를 기준으로 우선순위를 답하라.
- 직원/사람 조회(예: "김유정 누구야?", "유정님 소속/담당 뭐야?")는 Slack users.list/search.messages + Notion search 만 기준으로 답하라. 이 경우 Gmail·Drive·memory_read·local_read 는 쓰지 말라.
- 현재 활성 소스는 `{SOURCE_LABEL}`이다. 이 프로필에서 비활성화된 소스와 액션은 쓰지 마라.
- 답이 머릿속에 없으면 '확인이 필요합니다'로 끝내지 말고, 현재 활성 소스를 교차 조사해 근거(누가·언제·어디서)를 직접 찾아 완성된 답을 만들어라.
- 예: "이 알림 누가 처음 만들었나?" → Notion 백로그 발의자·작성일 + Slack 최초 논의 + 관련 메일/드라이브 문서를 교차해 "X가 YYYY-MM-DD에"까지 확정해 답한다.
- "확인 후 회신드리겠습니다"는 현재 활성 소스를 다 뒤져도 근거가 없을 때만, 무엇을 어디까지 찾았는지 밝히고 쓴다.
- "회의 어젠다/아젠다/미팅 준비/회의자료/녹음 기반 초안 만들어줘" 류는 안건 목록만 만들지 말고, 반드시 현재 활성 소스에서 관련 최신 문서·스레드·노션 페이지·근거를 찾아 *기초 자료*와 *참고출처/근거*까지 준비한다. Notion에 올릴 때도 본문에 `회의 목표`, `안건`, `기초 자료`, `참고출처` 섹션을 포함하라.

[너의 능력과 한계 — 아래 도구로 직접 조사하라]
- slack_read(method, query): 슬랙 읽기(발송 불가). method 허용: conversations.history, conversations.replies, conversations.info, conversations.list, users.info, users.list, search.messages. query 는 querystring. 예: method=conversations.replies, query='channel={CHANNEL}&ts=<스레드ts>'. **DM도 읽는다** — 'X가 나에게 준' 류는 users.list 로 상대 id 찾고 conversations.list(types=im)로 DM 채널 찾아 conversations.history.
- slack_files(channel): 특정 채널의 첨부 파일(PPT·PDF·엑셀) 목록(이름+[id:F…]+링크). **'채널 안 PPT/자료/문서 살펴봐'면 반드시 먼저 호출**(채널명 그대로, 예: DX사업부).
- slack_file_read(file_id): slack_files 의 [id:F…] 로 PPT/PDF 본문 텍스트를 읽는다. PPT 내용까지 근거로 답해야 하면 호출하라.
- recording_read(op, arg, include_transcript): 토리 미팅노트/녹음 전사 읽기. op=list|search|read. 회의·녹음·미팅노트 기반 초안/조사/자료정리는 search로 관련 회의를 찾고, 필요하면 read + include_transcript=true로 녹취록까지 확인하라.
- propose_send(channel, text, label, thread): 타인/다른 채널 발송이 필요하면 **직접 보내지 말고 이걸로 승인 제안만** 올려라 — 보스 ✅ 후 보스 명의로 발송된다. 대상 채널 id 는 slack_files(채널)·users.list+conversations.list(types=im, 사람 DM)로 먼저 찾는다.
- propose_gmail(to, subject, body): 메일 작성/회신이 필요하면 이걸로 **Gmail 초안 승인 제안**(보스 ✅ 후 보스 Gmail 에 초안 생성, 발송은 보스가).
- propose_calendar(summary, start, end, description, attendees): 일정 등록이 필요하면 이걸로 **캘린더 승인 제안**(보스 ✅ 후 등록). start/end='YYYY-MM-DD' 또는 'YYYY-MM-DDTHH:MM:SS'(KST).
- propose_notion(target_id, title, body, mode): 노션 페이지 생성/추가 **승인 제안**. 새 페이지 생성(mode=create)은 target_id 를 비워도 기본 부모 페이지 아래 새 페이지로 만든다. 특정 위치가 필요할 때만 target_id=부모 페이지 id 를 넣어라. 기존 페이지 추가(mode=append)는 target_id=기존 페이지 id 필수. 데이터베이스 id 는 새 문서 parent 로 쓰지 말고 task 생성은 propose_notion_task 를 써라. 조사 기반 문서는 가능한 한 `참고출처`/`근거` 섹션에 링크를 넣어라. 회의 어젠다/아젠다/미팅 준비 문서는 *회의 목표*, *안건*, *기초 자료*, *참고출처 링크*를 본문에 모두 넣어라.
- propose_notion_task(title, priority, due, status, categories): 보스가 'task/할 일 만들어줘'면 이걸로 **내 노션 task DB 에 생성 승인 제안**(담당={BOSS_NAME} 자동). priority='1'~'10'(낮을수록 우선) 또는 '상단 고정용'/'회의', due='YYYY-MM-DD'.
- gmail_read(op, arg): Gmail 읽기(발송·드래프트 불가). op: search '<gmail query>'(스레드 목록), thread '<thread_id>'(전문: 발신·제목·본문), message '<message_id>'.
  **메일 원문이 있으면 추측하지 말고 직접 읽어 정확히 답하라**(미납 금액·마감일·발신자명 등). 브리핑에 thread_id 가 보이면 그걸로 thread 를 열어라.
- notion_read(op, arg): 노션 읽기. op: search '<질의>', page '<id>', blocks '<id>'.
- drive_read(op, arg): 구글드라이브 읽기. op: search '<키워드>'(제목·본문 검색), read '<file_id>'(구글문서 본문).
- memory_read(query, scope): 회사 큐레이션 메모리 검색(IR·매출·KPI·재무·계약 등 확정 사실). scope=company(기본, 대외비)|personal.
- local_read(path): {ASSISTANT_NAME} 로컬 파일 읽기. state/slack-attention.json, feeds, deep-briefs 아래만.
- 발송·실행은 *승인 제안* 도구로(직접 실행 아님, 보스 ✅ 후 처리): 슬랙=propose_send, Gmail 초안=propose_gmail, 캘린더=propose_calendar. 메모리 저장·웹 검색만 아직 직접 불가(이때만 HANDOFF): 꼭 필요하면 본문에 "딥레인으로 넘김(최대 30분 내 처리)"이라 명시하고 끝에:
  [[HANDOFF]]해야 할 일을 자기완결적으로 1~3줄 (대상·내용·회신 위치 포함)[[/HANDOFF]]

[출력 규칙]
- 한국어, 결론부터, 2500자 이내. 사실과 (추정)을 구분하고 시각은 KST.
- 첫 줄 = 한 줄 결론(소제목·인사 없이 바로). 이어서 *소제목* 으로 묶고 핵심만 짧은 불릿 3~5개.
- 사고과정·메타설명(영어 'Now I have'/'Let me compile'/'Here is'/'From X:' 분석, 한국어 '충분한 자료를 확보'/'~작성하겠습니다' 류 서두) 없이 **첫 줄부터 결과물만**. 내부 추론은 출력하지 마라.
""" + READ_CORE + """- 회신 초안이 필요하면 "초안:" 으로 시작하는 블록으로 — 복사해 바로 쓸 수 있게.
- `to=slack_read` 같은 내부 도구 호출문, JSON 도구 인자, raw tool log 는 절대 출력하지 말라. 사용자는 최종 답변만 봐야 한다.
- 토큰·secret 값 출력 절대 금지.
- 이 메시지가 요청이 아니라면(완료 표시, 감탄, 잡담) 다른 말 없이 정확히 NO_REPLY 라고만 출력하라.
"""

# PROMPT_TMPL 의 %(필드)s 누락으로 호출부가 깨지지 않도록 기본값을 채워 포맷한다.
# (2026-06-15: replier 가 todo_context 를 안 넘겨 FULFILL 이 KeyError 로 죽던 회귀 수정 + intent/thread 주입 안전.)
_PROMPT_DEFAULTS = {"when": "", "request": "", "context": "(직전 맥락 없음)", "todo_context": "(스냅샷 없음)"}


def render_prompt(**kw):
    d = dict(_PROMPT_DEFAULTS)
    d.update({k: ("" if v is None else v) for k, v in kw.items()})
    return PROMPT_TMPL % d


AGENT_SYSTEM = (f"너는 {COMPANY_NAME} {BOSS_NAME} {BOSS_TITLE}의 비서 '{ASSISTANT_NAME}'다. 주어진 도구로 회사 데이터를 직접 조사해 "
                "근거 있는 답을 한국어로 만든다. 추측하지 말고 도구로 확인하라. "
                "**읽은 자료에 명시적으로 있는 것만 써라 — 역할·직책·R&R·수치·일정을 지어내거나 과거 관행으로 "
                "채우지 마라(미기재 역할 창작 금지). 같은 주제 여러 버전이면 최신만 근거로 삼고, 오래된 건 시점을 밝혀라.** "
                "회의 어젠다와 녹음 기반 초안 요청은 기초 자료·참고출처·녹취 근거까지 준비한다. "
                "직원/사람 조회는 Slack·Notion 만 근거로. 내부 도구 호출문은 절대 최종 출력하지 않는다. 아래 사용자 지침을 정확히 따르라.")


def run_claude(prompt, proxy_key):
    """claude -p 대체: :8321(VibeProxy) messages API + 파이썬 도구루프(tory_agent_llm).
    동일 계약 (ok, out, err) 유지 → watcher/replier 무수정. claude 바이너리·버전(502) 의존 제거.
    조사 엔진(BASE=:8321, 메모리=:1128)은 tory_agent_llm 의 TORY_LLM_BASE/TORYMEMORY_API env 로 라우팅."""
    try:
        import tory_agent_llm as agent
        out = (agent.run_agent(AGENT_SYSTEM, prompt, proxy_key, max_iters=24) or "").strip()
    except Exception as e:
        return False, "", "agent error: %s" % str(e)[:400]
    if not out:
        return False, "", "agent: empty output"
    if TOOL_LEAK_RE.search(out) or META_LEAK_RE.search(out):
        return False, "", "agent: internal/tool text leaked"
    return True, out, ""


HANDOFF_RE = re.compile(r"\[\[HANDOFF\]\](.*?)\[\[/HANDOFF\]\]", re.S)


def extract_handoffs(text, req):
    tasks = [t.strip() for t in HANDOFF_RE.findall(text) if t.strip()]
    cleaned = HANDOFF_RE.sub("", text).strip()
    if tasks:
        q = _read_json(HANDOFF_FILE, {"items": []})
        for t in tasks:
            q["items"].append({
                "created": datetime.now(KST).isoformat(timespec="seconds"),
                "request_channel": CHANNEL,
                "request_ts": req.get("ts"),
                "request_text": (req.get("text") or "")[:500],
                "task": t,
            })
        _write_json(HANDOFF_FILE, q)
        log("handoff queued:", len(tasks))
    return cleaned, len(tasks)


def react(token, ts, name, add=True):
    slack_call("reactions.add" if add else "reactions.remove", token,
               {"channel": CHANNEL, "timestamp": ts, "name": name}, post=True)


def post_thread(bot_token, ts, text, rich=False, footer=None):
    """스레드 회신. rich=True 면 Block Kit(헤더/섹션/구분선/컨텍스트)로 가독성↑,
    블록 실패·규격위반 시 text 로 폴백(알림·접근성 보존을 위해 항상 text 동봉)."""
    text = (text or "")[:MAX_REPLY]
    base = {"channel": CHANNEL, "thread_ts": ts, "text": text,
            "username": ASSISTANT_NAME, "icon_emoji": ASSISTANT_ICON,
            "unfurl_links": "false", "unfurl_media": "false"}
    if rich:
        try:
            blocks, fb = to_blocks(text, footer=footer)
            if blocks:
                p = dict(base, blocks=json.dumps(blocks, ensure_ascii=False), text=(fb or text)[:MAX_REPLY])
                r = slack_call("chat.postMessage", bot_token, p, post=True)
                if r.get("ok"):
                    return r
                log("blocks post failed → text fallback:", r.get("error"))
        except Exception as e:
            log("blocks build failed → text fallback:", str(e)[:120])
    return slack_call("chat.postMessage", bot_token, base, post=True)


def flush_outbox(env):
    """발신 단일 관문: Claude 레이어가 outbox 에 적재한 메시지를 토리 봇 명의로 발송.
    이 워처(헤르메스 스택)만이 실제 전송을 수행한다 — 킬스위치로 전 발신 정지 가능."""
    if _offwork_active():
        return
    bot = env.get("SLACK_BOT_TOKEN", "").strip() or env.get("SLACK_USER_TOKEN", "").strip()
    try:
        names = sorted(n for n in os.listdir(OUTBOX_DIR) if n.endswith(".json"))
    except FileNotFoundError:
        return
    for n in names:
        p = os.path.join(OUTBOX_DIR, n)
        msg = _read_json(p, None)
        if not msg or not (msg.get("text") or "").strip():
            os.replace(p, os.path.join(OUTBOX_FAILED, n))
            continue
        params = {"channel": msg.get("channel") or CHANNEL, "text": msg["text"][:MAX_REPLY],
                  "username": ASSISTANT_NAME, "icon_emoji": ASSISTANT_ICON,
                  "unfurl_links": "false", "unfurl_media": "false"}
        if msg.get("thread_ts"):
            params["thread_ts"] = msg["thread_ts"]
        r = slack_call("chat.postMessage", bot, params, post=True)
        if r.get("ok"):
            os.remove(p)
            log("outbox sent:", n)
        else:
            msg["attempts"] = int(msg.get("attempts", 0)) + 1
            msg["last_error"] = r.get("error")
            if msg["attempts"] >= 5:
                _write_json(os.path.join(OUTBOX_FAILED, n), msg)
                os.remove(p)
                log("outbox FAILED(final):", n, r.get("error"))
            else:
                _write_json(p, msg)
                log("outbox retry later:", n, r.get("error"))


def process(msg, all_msgs, env, state, thread_root=None):
    ts = msg["ts"]
    post_ts = thread_root or ts   # 스레드 답글이면 그 스레드 루트에 회신(대화 이어가기)
    bot = env.get("SLACK_BOT_TOKEN", "").strip()
    user_tok = env.get("SLACK_USER_TOKEN", "").strip()
    proxy_key = env.get("OPENAI_API_KEY", "").strip()
    react(bot or user_tok, ts, "eyes", add=True)
    req_text = (msg.get("text") or "").strip()
    if _is_onwork_request(req_text):
        _set_onwork()
        r = post_thread(bot or user_tok, post_ts,
                        "출근 모드로 전환했습니다. 브리핑·조사 제안을 다시 시작합니다.")
        if r.get("ok"):
            react(bot or user_tok, ts, "white_check_mark", add=True)
            react(bot or user_tok, ts, "eyes", add=False)
            state["claude_fail_streak"] = 0
            state.setdefault("active_threads", {})[post_ts] = ts
            _write_json(STATE_FILE, state)
        else:
            log("onwork post failed:", r.get("error"))
        return
    if _is_offwork_request(req_text):
        st = _set_offwork()
        r = post_thread(bot or user_tok, post_ts,
                        "퇴근 모드로 전환했습니다. 브리핑·조사 제안은 %s부터 다시 시작합니다." %
                        datetime.fromtimestamp(float(st["resume_at"]), KST).strftime("%Y-%m-%d %H:%M KST"))
        if r.get("ok"):
            react(bot or user_tok, ts, "white_check_mark", add=True)
            react(bot or user_tok, ts, "eyes", add=False)
            state["claude_fail_streak"] = 0
            state.setdefault("active_threads", {})[post_ts] = ts
            _write_json(STATE_FILE, state)
        else:
            log("offwork post failed:", r.get("error"))
        return
    if _is_credit_request(req_text):
        _request_credit(post_ts, ts)
        r = post_thread(bot or user_tok, post_ts, "💳 크레딧·사용량 조회 중입니다… 잠시만요(약 10초).")
        if r.get("ok"):
            react(bot or user_tok, ts, "eyes", add=False)
            state.setdefault("active_threads", {})[post_ts] = ts
            _write_json(STATE_FILE, state)
        else:
            log("credit ack post failed:", r.get("error"))
        return
    # ── 에이전트 팀: 분류기(triage) 우선 라우팅 ──
    #   의도를 먼저 파악해 경로를 정한다. 이름이 들어간 복잡한 조사/작성 요청('성낙천이 준 회의록
    #   참고해 승인안 작성')을 느슨한 직원조회 정규식이 가로채 엉뚱한 직원 todo 를 뱉던 버그
    #   (2026-06-15) 수정 — employee/todo 즉답은 분류기가 그 경로로 판정할 때만 탄다.
    ctx = build_context(all_msgs, ts, root_ts=thread_root)
    route, intent = "research", ""
    try:
        import tory_agent_llm as _agent
        tg = _agent.triage(req_text, ctx, proxy_key)
        route = tg.get("route") or "research"
        intent = (tg.get("intent") or "").strip()
        log("triage route=%s reply=%s%s" % (route, tg.get("reply"),
                                            " (fallback)" if tg.get("_fallback") else ""))
        if not tg.get("reply", True):
            react(bot or user_tok, ts, "eyes", add=False)
            state["claude_fail_streak"] = 0
            return
    except Exception as e:
        log("triage skipped:", str(e)[:120])
        route = "todo" if _is_todo_request(req_text) else "research"  # 폴백: 느슨한 직원정규식엔 의존 안 함
    # 직원 조회 즉답 — 분류기가 employee 로 보고 이름이 실제로 잡힐 때만(못 뽑으면 조사로 진행)
    if route == "employee":
        employee_name = _extract_employee_query(req_text)
        if employee_name:
            r = post_thread(bot or user_tok, post_ts, build_employee_answer(employee_name))
            if r.get("ok"):
                react(bot or user_tok, ts, "white_check_mark", add=True)
                react(bot or user_tok, ts, "eyes", add=False)
                state["claude_fail_streak"] = 0
                state.setdefault("active_threads", {})[post_ts] = ts
                _write_json(STATE_FILE, state)
            else:
                log("employee lookup post failed:", r.get("error"))
            return
    # 할 일/우선순위 즉답
    if route == "todo":
        r = post_thread(bot or user_tok, post_ts, build_todo_answer())
        if r.get("ok"):
            react(bot or user_tok, ts, "white_check_mark", add=True)
            react(bot or user_tok, ts, "eyes", add=False)
            state["claude_fail_streak"] = 0
            state.setdefault("active_threads", {})[post_ts] = ts
            _write_json(STATE_FILE, state)
        else:
            log("todo post failed:", r.get("error"))
        return
    thread_hint = ""
    if thread_root:
        thread_hint = ("[스레드 후속 지시] 이 메시지는 스레드 안 추가 지시다. 항목 번호(예: '3번')는 "
                       "이 스레드 첫 메시지(브리핑)의 번호 매핑을 따른다. 스레드 원문(브리핑 항목·회신 초안)이 "
                       "필요하면 slack_read(method=conversations.replies, query='channel=%s&ts=%s&limit=50')로 "
                       "직접 읽고 답하라." % (CHANNEL, thread_root))
    req_parts = [p for p in (thread_hint,
                             ("[핵심 의도 — 분류기 추정, 참고] " + intent) if intent else "",
                             req_text) if p]
    prompt = render_prompt(
        when=datetime.fromtimestamp(float(ts), KST).strftime("%m/%d %H:%M"),
        request="\n\n".join(req_parts),
        context=ctx,
        todo_context=build_todo_snapshot(),
    )
    log("claude run start ts=", ts)
    t0 = time.time()
    ok, out, err = run_claude(prompt, proxy_key)
    log("claude run done ok=%s %.0fs out=%dch" % (ok, time.time() - t0, len(out)))
    if ok and out.strip() == "NO_REPLY":
        react(bot or user_tok, ts, "eyes", add=False)
        state["claude_fail_streak"] = 0
        return
    if not ok or not out.strip():
        state["claude_fail_streak"] = int(state.get("claude_fail_streak", 0)) + 1
        react(bot or user_tok, ts, "eyes", add=False)
        post_thread(bot or user_tok, post_ts,
                    "(%s 즉시 처리 실패 — 30분 수신기가 이어받습니다. 원인: %s)" % (ASSISTANT_NAME, (err or "empty output")[:300]))
        log("claude FAIL:", err[:300])
        return
    cleaned, n_handoff = extract_handoffs(out, msg)
    cleaned = render_slack(cleaned)   # 공용 렌더: **→*, ##→*, [md](url)→<url|..>, 불릿 통일, 빈 섹션 제거
    if TOOL_LEAK_RE.search(cleaned) or META_LEAK_RE.search(cleaned):
        post_thread(bot or user_tok, post_ts,
                    "(%s 즉시 처리 실패 — 내부 작업 문장이 감지되어 발송을 차단했습니다. 30분 수신기가 이어받습니다.)" % ASSISTANT_NAME)
        log("post blocked: internal/tool text leaked")
        return
    r = post_thread(bot or user_tok, post_ts, cleaned, rich=True,
                    footer="%s · %s 조사" % (ASSISTANT_NAME, SOURCE_LABEL))
    if not r.get("ok"):
        log("postMessage failed:", r.get("error"))
        return
    react(bot or user_tok, ts, "white_check_mark", add=True)
    state["claude_fail_streak"] = 0
    # 이 스레드를 활성으로 등록 → 보스가 이어서 답글을 달면 다음 폴링이 받아 대화를 이어간다.
    state.setdefault("active_threads", {})[post_ts] = ts


def seed_brief_threads(state):
    """비서 채널의 최근 브리핑·게이트 메시지를 활성 스레드로 등록한다.
    기존엔 '토리가 답한 스레드'만 따라가서, 보스가 브리핑 스레드에 후속 지시('3번 더 자세히')를
    달면 watcher 가 못 봤다(톱레벨 history 는 thread 답글 누락). 브리핑 ts 를 active_threads 에
    심어 poll_threads 가 그 스레드의 보스 답글을 받게 한다. 시작점=브리핑 ts(과거 재생 없음)."""
    bs = _read_json(BRIEF_STATE, {})
    msgs = list(bs.get("brief_msgs") or [])
    if not msgs and bs.get("living_ts"):
        msgs = [{"ts": bs.get("living_ts"), "channel": bs.get("living_channel")}]
    at = state.setdefault("active_threads", {})
    changed = False
    for bm in msgs[-3:]:
        ts = bm.get("ts")
        ch = bm.get("channel")
        if not ts or (ch and ch != CHANNEL):
            continue
        if ts not in at:
            at[ts] = ts   # 그 이후 보스 답글만 처리
            changed = True
    return changed


def poll_threads(user_tok, env, state):
    """토리가 답한 활성 스레드에 보스가 새 답글을 달면 같은 스레드에서 이어받는다.
    톱레벨 history 폴링은 스레드 답글을 못 보므로(thread_ts!=ts), 활성 스레드만 골라
    conversations.replies 로 따로 확인한다. 24h 지난 스레드는 만료(폭주 방지)."""
    at = state.get("active_threads") or {}
    if not at:
        return
    now = time.time()
    for root in list(at.keys()):
        if now - float(root) > 86400:
            del at[root]
            continue
        rep = slack_call("conversations.replies", user_tok,
                         {"channel": CHANNEL, "ts": root, "limit": 50})
        if not rep.get("ok"):
            continue
        rmsgs = sorted(rep.get("messages") or [], key=lambda x: float(x.get("ts", 0)))
        last = at.get(root, root)
        for rm in rmsgs:
            if float(rm.get("ts", 0)) <= float(last):
                continue
            if rm.get("user") == BOSS and not rm.get("subtype") and not rm.get("bot_id"):
                t = (rm.get("text") or "").strip()
                if t and not is_noise(t):
                    log("thread reply ts=%s root=%s" % (rm["ts"], root))
                    process(rm, rmsgs, env, state, thread_root=root)
            at[root] = rm["ts"]
    state["active_threads"] = at
    _write_json(STATE_FILE, state)


def main():
    once = "--once" in sys.argv
    if assistant_config:
        assistant_config.ensure_profile_dirs(PROFILE)
    single_instance("command-watcher")
    os.makedirs(WORKDIR, exist_ok=True)
    os.makedirs(os.path.dirname(HANDOFF_FILE), exist_ok=True)
    os.makedirs(OUTBOX_FAILED, exist_ok=True)
    env = load_env(ENV_FILE)
    user_tok = env.get("SLACK_USER_TOKEN", "").strip()
    if not (CHANNEL and BOSS):
        log("assistant profile missing assistant_channel_id/boss_user_id — exit")
        sys.exit(1)
    if not user_tok:
        log("SLACK_USER_TOKEN not set — exit")
        sys.exit(1)
    if not env.get("OPENAI_API_KEY", "").strip():
        log("OPENAI_API_KEY(:8321 proxy key) not set — exit")
        sys.exit(1)
    state = _read_json(STATE_FILE, {})
    if not state.get("last_ts"):
        state["last_ts"] = "%.6f" % time.time()  # 첫 기동: 과거 재생 금지
        _write_json(STATE_FILE, state)
    log("watcher started. channel=%s poll=%ss" % (CHANNEL, POLL_SEC))
    while True:
        try:
            flush_outbox(env)
            oldest = max(float(state.get("last_ts", 0)), time.time() - BACKLOG_CAP_SEC)
            resp = slack_call("conversations.history", user_tok,
                              {"channel": CHANNEL, "oldest": "%.6f" % oldest, "limit": 25})
            if resp.get("ok"):
                msgs = sorted(resp.get("messages") or [], key=lambda m: float(m.get("ts", 0)))
                for m in msgs:
                    if float(m.get("ts", 0)) <= float(state.get("last_ts", 0)):
                        continue
                    if is_request(m):
                        process(m, msgs, env, state)
                    state["last_ts"] = m["ts"]
                    _write_json(STATE_FILE, state)
            else:
                log("history error:", resp.get("error"))
            seed_brief_threads(state)            # 브리핑 스레드도 활성 등록 → 후속 지시 수신
            poll_threads(user_tok, env, state)   # 활성 스레드 보스 답글 이어받기(브리핑 스레드 포함)
            try:                                 # 승인 게이트 발송: 보스 ✅ 한 제안만 원문으로 발송
                import tory_send_gate
                tory_send_gate.process(env)
            except Exception as e:
                log("send_gate error:", repr(e))
        except Exception as e:
            log("loop error:", repr(e))
        if once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
