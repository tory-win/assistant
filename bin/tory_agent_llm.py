#!/usr/bin/env python3
"""
tory_agent_llm.py — claude -p(바이너리) 대체. :8321(VibeProxy) messages API + 도구 루프를 파이썬으로.

claude 바이너리·버전(502) 의존 제거 → 도커가 python 만으로 가벼워짐. 토리(watcher/replier)의 조사 엔진.
tools = 4소스 읽기 헬퍼(slack/gmail/notion/drive). claude 가 tool_use 하면 헬퍼 실행 → 결과 반환 → 반복 → 최종 텍스트.

run_agent(system, user, key) → 최종 답변 텍스트 (claude -p 의 (ok,out,err) 와 달리 텍스트만 반환).
"""
import os
import json
import re
import time
import subprocess
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".torymemory", "bin")
BASE = os.environ.get("TORY_LLM_BASE", "http://localhost:8321")   # :8321 프록시 전용. 앱/세션의 ANTHROPIC_BASE_URL(진짜 Anthropic) 오염 회피.
PRIMARY_MODEL = os.environ.get("TORY_AGENT_MODEL", "claude-opus-4-6")
GPT_FALLBACK_MODELS = [m.strip() for m in os.environ.get("TORY_AGENT_GPT_FALLBACK_MODELS", "gpt-5.4").split(",") if m.strip()]
TMEM_API = os.environ.get("TORYMEMORY_API", "http://localhost:1128")   # 회사 큐레이션 메모리(:1128). 도커는 host.docker.internal:1128.

try:
    import tory_assistant_config as _ac
    PROFILE = _ac.load_profile()
except Exception:
    PROFILE = {
        "assistant_name": "토리",
        "boss_name": "오승현",
        "boss_title": "전략본부장",
        "company_name": "ASWEMAKE",
    }

ASSISTANT_NAME = PROFILE.get("assistant_name") or "토리"
BOSS_NAME = PROFILE.get("boss_name") or "오승현"
BOSS_TITLE = PROFILE.get("boss_title") or "전략본부장"
COMPANY_NAME = PROFILE.get("company_name") or "ASWEMAKE"
ENABLED_SOURCES = set((PROFILE.get("enabled_sources") or
                       ["slack", "gmail", "calendar", "drive", "notion", "memory", "local", "recordings"]))
ENABLED_ACTIONS = set((PROFILE.get("enabled_actions") or ["slack", "gmail", "calendar", "notion"]))

TOOLS = [
    {"name": "slack_read", "description": "회사 슬랙 읽기(읽기전용). method: conversations.history, conversations.replies, conversations.info, conversations.list, users.info, users.list, search.messages. **DM도 읽을 수 있다** — '누가 나에게 준/보낸/공유한' 류는 users.list 로 상대 user id 를 찾고 conversations.list(query='types=im') 로 그 사람과의 DM 채널을 찾아 conversations.history 로 읽어라. 특정인이 공유한 링크/회의록은 search.messages(query='from:<id> 키워드') 로도 찾는다.",
     "input_schema": {"type": "object", "properties": {
         "method": {"type": "string"},
         "query": {"type": "string", "description": "querystring. 예: channel=C123&ts=1700000000.000"}},
         "required": ["method", "query"]}},
    {"name": "gmail_read", "description": "회사 Gmail 읽기. op=search('<gmail query>')|thread('<id>')|message('<id>')",
     "input_schema": {"type": "object", "properties": {"op": {"type": "string"}, "arg": {"type": "string"}}, "required": ["op", "arg"]}},
    {"name": "notion_read", "description": "회사 노션 읽기. op=search('<질의>')|page('<id>')|blocks('<id>')",
     "input_schema": {"type": "object", "properties": {"op": {"type": "string"}, "arg": {"type": "string"}}, "required": ["op", "arg"]}},
    {"name": "drive_read", "description": "구글드라이브 읽기. op=search('<키워드>')|read('<file_id>')",
     "input_schema": {"type": "object", "properties": {"op": {"type": "string"}, "arg": {"type": "string"}}, "required": ["op", "arg"]}},
    {"name": "memory_read", "description": "회사 큐레이션 메모리 검색. IR 답변·매출·KPI·재무·계약/협업 구조 등 확정 정본 사실은 라이브 4소스가 아니라 여기 있다. scope=company(기본, 대외비 awm_confidential)|personal(tory)",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "scope": {"type": "string"}}, "required": ["query"]}},
    {"name": "local_read", "description": "%s 로컬 상태/피드/브리프 파일 읽기(읽기전용). path 는 state/feeds/deep-briefs 아래만. 예: state/slack-attention.json, feeds, deep-briefs. 디렉터리면 목록을 준다." % ASSISTANT_NAME,
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "recording_read", "description": "토리 미팅노트/녹음 전사 읽기(읽기전용). op=list|search|read. list는 최근 회의록 목록, search는 제목·요약·녹취록 검색, read는 특정 note_id/제목의 미팅노트와 필요시 전체 녹취록을 읽는다. 회의 기반 초안·조사·어젠다에는 Slack/Gmail/Notion/Drive와 함께 이 도구를 우선 확인하라.",
     "input_schema": {"type": "object", "properties": {
         "op": {"type": "string"},
         "arg": {"type": "string", "description": "search query 또는 note_id/title"},
         "include_transcript": {"type": "boolean", "description": "read에서 전체 녹취록까지 필요하면 true"}},
         "required": ["op"]}},
    {"name": "slack_files", "description": "특정 슬랙 채널에 올라온 첨부 파일(PPT·PDF·엑셀 등) 목록을 본다. '채널 안의 PPT/자료/문서 살펴봐' 류 요청에 반드시 쓴다. channel 은 채널명(예: DX사업부) 또는 채널 id. 결과의 각 파일에는 [id:F...] 가 붙는다 — 본문이 필요하면 그 id 로 slack_file_read 를 호출하라(특히 '조직개편안' 등 제목이 핵심에 부합하는 파일).",
     "input_schema": {"type": "object", "properties": {"channel": {"type": "string"}}, "required": ["channel"]}},
    {"name": "slack_file_read", "description": "슬랙 파일(PPT·PDF·엑셀) 본문 텍스트를 읽는다. file_id 는 slack_files 결과의 [id:F...] 값. 변환 PDF 를 텍스트로 추출해 돌려준다(files:read 스코프 있을 때). PPT 내용까지 근거로 답해야 하면 이걸 써라.",
     "input_schema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}},
    {"name": "propose_send", "description": "메시지를 타인/다른 채널에 보내는 **승인 제안**을 올린다(직접 발송 아님). 비서 채널에 '📤 발송 승인 요청'을 띄우고 보스가 ✅ 해야만 보스 명의로 발송된다. 보스가 '~에게 보내줘 / 이 답장 보내줘' 처럼 발송을 원할 때만 쓴다. channel=대상 채널 id(C…/D…; 채널명은 slack_files 로, 사람은 users.list+conversations.list(query='types=im') 로 DM 채널 id 를 먼저 찾아라). text=보낼 내용(보스 명의로 나가니 1인칭·완성문). label=무엇/누구에게인지 한 줄. thread=원문 스레드 ts(스레드 답장이면).",
     "input_schema": {"type": "object", "properties": {"channel": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "string"}, "thread": {"type": "string"}}, "required": ["channel", "text"]}},
    {"name": "propose_gmail", "description": "Gmail *초안* 생성 **승인 제안**을 올린다(직접 발송 아님). 보스 ✅ 후 보스 Gmail 에 초안이 생긴다(발송은 보스가 직접). 메일 작성/회신이 필요할 때. to=받는사람 이메일, subject=제목, body=본문(보스 명의 1인칭 완성문).",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
    {"name": "propose_calendar", "description": "캘린더 일정 등록 **승인 제안**을 올린다. 보스 ✅ 후 보스 캘린더(primary)에 일정 생성. summary=제목, start/end='YYYY-MM-DD'(종일) 또는 'YYYY-MM-DDTHH:MM:SS'(시간 지정, KST), description=설명(선택), attendees=참석자 이메일 배열(선택).",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}, "attendees": {"type": "array", "items": {"type": "string"}}}, "required": ["summary", "start", "end"]}},
    {"name": "propose_notion", "description": "노션 페이지 생성/추가 **승인 제안**을 올린다(직접 실행 아님). 보스 ✅ 후 실행. mode='create'(기본)는 target_id 를 비워도 기본 부모 페이지 아래 새 페이지(title+body)를 만든다. 특정 위치가 필요할 때만 target_id=부모 페이지 id 를 넣어라. mode='append'는 target_id=기존 페이지 id 가 필수다. 데이터베이스 id 는 새 문서 parent 로 쓰지 말고, task 생성은 propose_notion_task 를 써라. 조사 기반 문서는 가능한 한 참고출처/근거 섹션에 링크를 넣어라. 회의 어젠다/아젠다/미팅 준비 문서는 목표, 안건, 기초 자료, 참고출처 링크를 body에 모두 넣어야 하며, 빠지면 도구가 제안을 보류한다.",
     "input_schema": {"type": "object", "properties": {"target_id": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}, "mode": {"type": "string", "enum": ["create", "append"]}}, "required": ["body"]}},
    {"name": "propose_notion_task", "description": "보스의 노션 task DB(전사 업무)에 **task 생성 승인 제안**(담당자=%s 자동). 보스 ✅ 후 생성. 보스가 '~ task 만들어줘/추가해줘' 류일 때. title=업무 이름(필수). priority=우선순위('1'~'10' 숫자 — 낮을수록 높은 우선, 또는 '상단 고정용'/'회의'). due='YYYY-MM-DD'(마감). status='예정'(기본)/'진행중'. categories=분류 배열(선택, 예: ['TF','본부 업무','미팅'])." % BOSS_NAME,
     "input_schema": {"type": "object", "properties": {"title": {"type": "string"}, "priority": {"type": "string"}, "due": {"type": "string"}, "status": {"type": "string"}, "categories": {"type": "array", "items": {"type": "string"}}}, "required": ["title"]}},
]

_TOOL_SOURCE = {
    "slack_read": "slack",
    "slack_files": "slack",
    "slack_file_read": "slack",
    "gmail_read": "gmail",
    "notion_read": "notion",
    "drive_read": "drive",
    "memory_read": "memory",
    "local_read": "local",
    "recording_read": "recordings",
}
_TOOL_ACTION = {
    "propose_send": "slack",
    "propose_gmail": "gmail",
    "propose_calendar": "calendar",
    "propose_notion": "notion",
    "propose_notion_task": "notion",
}


def _tool_enabled(name):
    src = _TOOL_SOURCE.get(name)
    if src and src not in ENABLED_SOURCES:
        return False
    act = _TOOL_ACTION.get(name)
    if act and act not in ENABLED_ACTIONS:
        return False
    return True


TOOLS = [t for t in TOOLS if _tool_enabled(t["name"])]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
TEXT_TOOL_RE = re.compile(r"\bto\s*=\s*(%s)\b" % "|".join(re.escape(n) for n in TOOL_NAMES))
META_LEAK_RE = re.compile(
    r"(The test worked|body parameter was the issue|Let me create the actual report|"
    r"I should not submit another|Since I already used|Actually,?\s+looking at the error|"
    r"test proposal went through|provide the full report in my response|"
    r"what will be posted as a thread reply)",
    re.I,
)

_LOCAL_ROOTS = [os.path.join(HOME, ".torymemory", d) for d in ("state", "feeds", "deep-briefs")]
_STATE_DIR = PROFILE.get("state_dir") if isinstance(PROFILE, dict) else ""
_STATE_DIR = _STATE_DIR or os.path.join(HOME, ".torymemory", "state")
_RECORDING_INDEX = os.path.join(_STATE_DIR, "recordings-index.json")
_RECORDING_DIR = os.path.join(_STATE_DIR, "recordings")


def _run(cmd, cap=6000):
    try:
        env = os.environ.copy()
        if PROFILE.get("env_file"):
            env.setdefault("TORY_ENV_FILE", PROFILE["env_file"])
        if PROFILE.get("id"):
            env.setdefault("TORY_ASSISTANT_ID", PROFILE["id"])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
        return (r.stdout or r.stderr or "(빈 결과)")[:cap]
    except Exception as e:
        return "도구 실행 실패: %s" % str(e)[:140]


def _mem_filter(user_id, size, search_query=None, page=1):
    body = {"user_id": user_id, "page": page, "size": size,
            "sort_column": "created_at", "sort_direction": "desc", "show_archived": False}
    if search_query:
        body["search_query"] = search_query[:300]
    req = urllib.request.Request(TMEM_API.rstrip("/") + "/api/v1/memories/filter",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.load(r)
    return out.get("items", []) if isinstance(out, dict) else []


def _mem_search(query, user_id, limit=8):
    """회사 큐레이션 메모리 검색. 서버 검색이 비면(벡터검색 불안정) 최근 풀을 받아 키워드로 직접 매칭.
    실패해도 조사는 라이브 4소스로 진행(best-effort)."""
    try:
        items = _mem_filter(user_id, limit, query)            # 1) 서버 검색
        if not items:                                          # 2) 폴백: 전체 풀 페이지네이션 + 클라이언트 키워드 매칭
            toks = [t for t in (query or "").split() if len(t) >= 2]
            pool, page = [], 1
            while page <= 5:                                   # size 100 × 최대 5p = 500건 (회사 풀 ~310 커버)
                batch = _mem_filter(user_id, 100, page=page)
                if not batch:
                    break
                pool.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            scored = []
            for it in pool:
                c = it.get("content", "")
                if not c:
                    continue
                s = sum(1 for t in toks if t in c)
                if s:
                    scored.append((s, c))
            scored.sort(key=lambda x: -x[0])
            items = [{"content": c} for _, c in scored[:limit]]
        rows = [it.get("content", "")[:300] for it in items if it.get("content")]
        return ("\n".join("- " + x for x in rows) or "(메모리 결과 없음)")[:6000]
    except Exception as e:
        return "메모리 검색 실패: %s" % str(e)[:140]


def _local_read(path):
    """경로 제한 로컬 읽기: state/feeds/deep-briefs 아래만. 디렉터리는 목록."""
    try:
        p = path if os.path.isabs(path) else os.path.join(HOME, ".torymemory", path)
        p = os.path.realpath(p)
        if not any(p == r or p.startswith(r + os.sep) for r in _LOCAL_ROOTS):
            return "허용되지 않은 경로(state/feeds/deep-briefs 만): %s" % path
        if os.path.isdir(p):
            return "\n".join(sorted(os.listdir(p)))[:4000] or "(빈 디렉터리)"
        with open(p, "r", errors="replace") as f:
            return f.read()[:8000]
    except Exception as e:
        return "로컬 읽기 실패: %s" % str(e)[:140]


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _recording_index():
    data = _read_json(_RECORDING_INDEX, {})
    notes = data.get("notes") if isinstance(data, dict) else None
    return notes if isinstance(notes, list) else []


def _recording_note(entry):
    path = entry.get("json_path") or ""
    fallback = os.path.join(_RECORDING_DIR, re.sub(r"[^A-Za-z0-9_.-]+", "-", entry.get("id") or "").strip("-") + ".json")
    if not (path and os.path.exists(path)):
        path = fallback
    return _read_json(path, {})


def _recording_find(arg):
    arg = (arg or "").strip()
    if not arg:
        return None
    low = arg.lower()
    for entry in _recording_index():
        if low == (entry.get("id") or "").lower():
            return entry
    for entry in _recording_index():
        title = (entry.get("title") or "").lower()
        if low and (low in title or title in low):
            return entry
    return None


def _recording_tokens(query):
    toks = []
    for tok in re.split(r"\s+", (query or "").strip().lower()):
        tok = tok.strip()
        if len(tok) >= 2:
            toks.append(tok)
    if not toks and query:
        toks.append(query.strip().lower())
    return toks


def _recording_excerpt(text, toks, cap=520):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    low = text.lower()
    pos = min([low.find(t) for t in toks if t and low.find(t) >= 0] or [0])
    start = max(0, pos - 160)
    end = min(len(text), pos + cap)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _recording_list(limit=10):
    idx = _recording_index()
    if not idx:
        return "녹음/미팅노트 인덱스가 없습니다. tory_recording_fetch.py 동기화가 필요합니다."
    rows = []
    for entry in idx[:limit]:
        dur = entry.get("duration_seconds") or 0
        try:
            dur = "%d분" % max(0, int(float(dur)) // 60)
        except Exception:
            dur = "?"
        rows.append("- [id:%s] %s · %s · updated %s · %s\n  %s" % (
            entry.get("id"), entry.get("title"), entry.get("status") or "?",
            (entry.get("updated_at") or "?")[:16], dur, entry.get("one_line") or ""))
    return "토리 녹음/미팅노트 최근 %d건:\n%s" % (len(rows), "\n".join(rows))


def _recording_search(query, limit=6):
    toks = _recording_tokens(query)
    if not toks:
        return _recording_list()
    scored = []
    for entry in _recording_index():
        note = _recording_note(entry)
        text = "\n".join([
            entry.get("title") or "",
            entry.get("one_line") or "",
            note.get("search_text") or "",
        ])
        low = text.lower()
        score = sum(low.count(t) for t in toks if t)
        if score:
            scored.append((score, entry, note, _recording_excerpt(text, toks)))
    scored.sort(key=lambda x: (-x[0], x[1].get("updated_at") or ""))
    if not scored:
        return "녹음/미팅노트 검색 결과 없음: %s" % query
    rows = []
    for score, entry, note, excerpt in scored[:limit]:
        rows.append("- [id:%s] %s · score %s · updated %s\n  %s" % (
            entry.get("id"), entry.get("title"), score, (entry.get("updated_at") or "?")[:16],
            excerpt or (entry.get("one_line") or "")))
    return "녹음/미팅노트 검색 결과:\n%s\n필요하면 recording_read(op='read', arg='<id>', include_transcript=true)로 원문을 읽어라." % "\n".join(rows)


def _recording_read(op="list", arg="", include_transcript=False):
    op = (op or "list").strip().lower()
    if op in ("list", "recent"):
        return _recording_list()
    if op == "search":
        return _recording_search(arg)
    if op == "read":
        entry = _recording_find(arg)
        if not entry:
            return "녹음/미팅노트 note_id/title을 못 찾음: %s" % (arg or "(빈 값)")
        note = _recording_note(entry)
        md_path = entry.get("markdown_path")
        fallback_md = os.path.join(_RECORDING_DIR, re.sub(r"[^A-Za-z0-9_.-]+", "-", entry.get("id") or "").strip("-") + ".md")
        if not (md_path and os.path.exists(md_path)):
            md_path = fallback_md
        text = ""
        try:
            if md_path:
                with open(md_path, encoding="utf-8") as f:
                    text = f.read()
        except Exception:
            text = ""
        if not text:
            text = json.dumps(note, ensure_ascii=False, indent=1)
        if not include_transcript:
            text = re.sub(r"\n## 녹취록\n.*", "\n## 녹취록\n\n(전체 녹취록은 include_transcript=true 로 다시 읽기)\n", text, flags=re.S)
        return text[:30000]
    return "알 수 없는 recording_read op: %s" % op


def _slack_files(channel, limit=200):
    """채널 첨부 파일(PPT/PDF/엑셀 등) 목록. 본문은 files:read 스코프 없어 미열람 — 이름+permalink 만.
    conversations.history 안의 files 메타라 추가 스코프 불필요. 채널명(한글 NFC/NFD·공백 무관)도 해석."""
    import unicodedata

    def _norm(s):
        return unicodedata.normalize("NFC", s or "").lower().replace(" ", "")

    ch = (channel or "").strip().lstrip("#")
    cid = ch if re.match(r"^[CGD][A-Z0-9]{6,}$", ch) else None
    if not cid:
        want = _norm(ch)
        cands, cursor = [], ""
        for _ in range(6):   # 페이지네이션(채널 다수 — 327+). 정확일치 우선.
            q = "types=public_channel,private_channel&limit=1000" + (("&cursor=" + cursor) if cursor else "")
            try:
                d = json.loads(_run(["bash", os.path.join(BIN, "tory_slack_read.sh"), "conversations.list", q], cap=400000))
            except Exception:
                break
            for c in d.get("channels", []):
                nm = _norm(c.get("name"))
                if want and (want in nm or nm in want):
                    score = 0 if nm == want else (1 if nm.startswith(want) else 2)
                    cands.append((score, len(nm), c.get("id")))
            cursor = (d.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        if not cands:
            return "채널을 못 찾음: %s" % channel
        cands.sort()
        cid = cands[0][2]
    out = _run(["bash", os.path.join(BIN, "tory_slack_read.sh"), "conversations.history",
                "channel=%s&limit=%d" % (cid, min(limit, 200))], cap=5000000)
    try:
        msgs = json.loads(out).get("messages", [])
    except Exception:
        return "채널 히스토리 읽기 실패: %s" % cid
    import datetime
    seen, rows = set(), []
    for m in msgs:  # conversations.history 는 최신순 → rows 도 최신순(첫 등장=최신 유지)
        ts = m.get("ts")
        try:
            dt = datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d") if ts else "?"
        except Exception:
            dt = "?"
        for f in (m.get("files") or []):
            n = unicodedata.normalize("NFC", f.get("name") or "")
            if not n or n in seen:
                continue
            seen.add(n)
            rows.append("- (공유 %s) [id:%s] %s [%s] %s" % (dt, f.get("id") or "?", n, f.get("filetype") or "?", f.get("permalink") or ""))
    if not rows:
        return "채널 %s 최근 글에 첨부 파일이 없습니다." % cid
    return ("채널 %s 첨부 파일 %d개 (최신순). **같은 주제 여러 버전이면 공유일이 가장 최근인 것을 쓰고, 오래된 "
            "자료는 현재 사실로 단정하지 마라.** 본문이 필요하면 [id:F...] 로 slack_file_read 호출:\n%s"
            % (cid, len(rows), "\n".join(rows[:40])))


def _propose_send(channel, text, label="", thread=None):
    """발송 '승인 제안'을 비서 채널에 올린다(직접 발송 아님). 보스 ✅ 후에만 send_gate 가 발송."""
    if not (channel and (text or "").strip()):
        return "발송 제안 실패: channel 과 text 가 필요합니다."
    try:
        import tory_send_gate as sg
        import tory_command_watcher as cw
        env = cw.load_env(cw.ENV_FILE)
        bot = env.get("SLACK_BOT_TOKEN", "").strip()
        ts = sg.propose(bot, channel, text, label or channel, target_thread=thread)
        if ts:
            return ("발송 승인 요청을 비서 채널에 올렸습니다. 보스가 ✅ 하면 그때 '%s'에 보스 명의로 발송됩니다 "
                    "(승인 전에는 절대 안 나갑니다). 너는 추가 발송 시도를 하지 마라." % (label or channel))
        return "발송 제안 게시 실패 — 채널 id/토큰을 확인하세요."
    except Exception as e:
        return "발송 제안 오류: %s" % str(e)[:140]


def _propose_gmail(to, subject, body):
    if not (to and subject and (body or "").strip()):
        return "Gmail 제안 실패: to/subject/body 가 모두 필요합니다."
    try:
        import tory_send_gate as sg
        import tory_command_watcher as cw
        bot = cw.load_env(cw.ENV_FILE).get("SLACK_BOT_TOKEN", "").strip()
        preview = "받는사람: %s\n제목: %s\n---\n%s" % (to, subject, (body or "")[:800])
        ts = sg.propose_action(bot, "Gmail 초안: %s → %s" % (subject, to), preview,
                               {"type": "gmail_draft", "to": to, "subject": subject, "body": body})
        return ("Gmail 초안 승인 요청을 올렸습니다. 보스 ✅ 후 초안이 생성됩니다(발송은 보스가 직접). 추가 시도 금지."
                if ts else "Gmail 제안 게시 실패.")
    except Exception as e:
        return "Gmail 제안 오류: %s" % str(e)[:140]


def _propose_calendar(summary, start, end, description="", attendees=None):
    if not (summary and start and end):
        return "일정 제안 실패: summary/start/end 가 필요합니다."
    try:
        import tory_send_gate as sg
        import tory_command_watcher as cw
        bot = cw.load_env(cw.ENV_FILE).get("SLACK_BOT_TOKEN", "").strip()
        preview = "일정: %s\n시작: %s\n종료: %s%s" % (
            summary, start, end, ("\n참석: " + ", ".join(attendees)) if attendees else "")
        ts = sg.propose_action(bot, "캘린더: %s" % summary, preview,
                               {"type": "calendar_event", "summary": summary, "start": start, "end": end,
                                "description": description or "", "attendees": attendees or []})
        return ("일정 등록 승인 요청을 올렸습니다. 보스 ✅ 후 캘린더에 등록됩니다. 추가 시도 금지."
                if ts else "일정 제안 게시 실패.")
    except Exception as e:
        return "일정 제안 오류: %s" % str(e)[:140]


_AGENDA_DOC_RE = re.compile(r"(어젠다|아젠다|agenda|회의\s*안건|회의\s*준비|미팅\s*준비|회의\s*자료|회의자료)", re.I)
_AGENDA_SECTION_RE = re.compile(r"(안건|어젠다|아젠다|agenda)", re.I)
_PREP_SECTION_RE = re.compile(r"(기초\s*자료|사전\s*자료|준비\s*자료|배경\s*자료|읽을\s*거리|자료\s*준비)", re.I)
_SOURCE_SECTION_RE = re.compile(r"(참고\s*출처|참고\s*자료|근거|출처|reference|source)", re.I)
_URL_RE = re.compile(r"https?://")


def _agenda_missing(title, body):
    text = "%s\n%s" % (title or "", body or "")
    if not _AGENDA_DOC_RE.search(text):
        return []
    missing = []
    if not _AGENDA_SECTION_RE.search(body or ""):
        missing.append("안건/어젠다")
    if not _PREP_SECTION_RE.search(body or ""):
        missing.append("기초 자료")
    if not _SOURCE_SECTION_RE.search(body or ""):
        missing.append("참고출처/근거")
    if not _URL_RE.search(body or ""):
        missing.append("출처 링크")
    return missing


def _is_test_notion_payload(title, body):
    title_s = re.sub(r"\s+", " ", title or "").strip().lower()
    body_s = re.sub(r"\s+", " ", body or "").strip().lower()
    return title_s in {"test", "테스트", "제목 없음"} and body_s in {"test", "테스트", ""}


def _notion_object_kind(target_id):
    target_id = (target_id or "").strip()
    if not target_id:
        return "missing"
    try:
        env = {}
        try:
            import tory_command_watcher as cw
            env = cw.load_env(cw.ENV_FILE)
        except Exception:
            env = {}
        tok = (env.get("NOTION_TOKEN") or "").strip()
        if not tok:
            return "no_token"
        for kind, url in (
            ("page", "https://api.notion.com/v1/pages/%s" % target_id),
            ("database", "https://api.notion.com/v1/databases/%s" % target_id),
        ):
            req = urllib.request.Request(
                url,
                headers={"Authorization": "Bearer " + tok, "Notion-Version": "2022-06-28"},
            )
            try:
                with urllib.request.urlopen(req, timeout=12):
                    return kind
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    continue
                return "error"
            except Exception:
                return "error"
    except Exception:
        return "error"
    return "not_found"


def _default_notion_parent_page_id():
    return ((PROFILE.get("notion_default_parent_page_id")
             or os.environ.get("TORY_NOTION_DEFAULT_PARENT_PAGE_ID")
             or "").strip())


def _propose_notion(target_id, title, body, mode="create"):
    mode = mode or "create"
    target_id = (target_id or "").strip()
    if not (body or "").strip():
        return "노션 제안 실패: body 가 필요합니다."
    if _is_test_notion_payload(title, body):
        return "노션 제안 차단: test/빈 제목 테스트 페이지는 만들지 않습니다. 추가 도구 호출 없이 사용자에게 본문을 직접 답하세요."
    default_parent = _default_notion_parent_page_id()
    if mode == "create" and not target_id:
        target_id = default_parent
    if mode == "append" and not target_id:
        return "노션 제안 실패: 기존 페이지에 추가하려면 target_id 가 필요합니다."
    kind = _notion_object_kind(target_id)
    if mode == "create" and kind == "database" and default_parent and default_parent != target_id:
        target_id = default_parent
        kind = _notion_object_kind(target_id)
    if mode in ("create", "append") and kind != "page":
        return ("노션 제안 차단: target_id=%s 는 %s 입니다. 이 도구는 실제 Notion 페이지 id 만 받습니다. "
                "데이터베이스 task 생성은 propose_notion_task를 쓰고, 보고서 페이지는 notion_read(search/page)로 "
                "공유된 부모 페이지를 찾은 뒤 진행하세요. 추가 도구 호출 없이 사용자에게 원인과 본문을 답하세요."
                % (target_id, kind))
    missing = _agenda_missing(title, body)
    if missing:
        return ("노션 제안 보류: 어젠다 문서는 %s 이/가 필요합니다. Slack/Gmail/Notion/Drive를 먼저 찾아 "
                "회의 목표, 안건, 기초 자료(읽어볼 문서·최근 논의·결정사항), 참고출처 링크를 body에 넣은 뒤 "
                "propose_notion을 다시 호출하세요." % ", ".join(missing))
    try:
        import tory_send_gate as sg
        import tory_command_watcher as cw
        bot = cw.load_env(cw.ENV_FILE).get("SLACK_BOT_TOKEN", "").strip()
        if mode == "append":
            action = {"type": "notion_append", "page_id": target_id, "title": title or "", "body": body}
            label = "노션 추가: %s" % (title or target_id)
            head = "기존 페이지에 추가"
        else:
            action = {"type": "notion_page", "parent_id": target_id, "title": title or "제목 없음", "body": body}
            label = "노션 새 페이지: %s" % (title or "제목 없음")
            head = "새 페이지 생성"
        preview = "%s\n제목: %s\n---\n%s" % (head, title or "(없음)", (body or "")[:700])
        ts = sg.propose_action(bot, label, preview, action)
        return ("노션 승인 요청을 올렸습니다. 보스 ✅ 후 실행됩니다. 추가 시도 금지." if ts else "노션 제안 게시 실패.")
    except Exception as e:
        return "노션 제안 오류: %s" % str(e)[:140]


def _propose_notion_task(title, priority=None, due=None, status=None, categories=None):
    if not (title or "").strip():
        return "task 제안 실패: title 이 필요합니다."
    try:
        import tory_send_gate as sg
        import tory_command_watcher as cw
        bot = cw.load_env(cw.ENV_FILE).get("SLACK_BOT_TOKEN", "").strip()
        preview = "업무: %s\n우선순위: %s · 마감: %s · 상태: %s%s" % (
            title, priority or "(없음)", due or "(없음)", status or "예정",
            ("\n분류: " + ", ".join(categories)) if categories else "")
        ts = sg.propose_action(bot, "노션 task 생성: %s" % title, preview,
                               {"type": "notion_task", "title": title, "priority": priority, "due": due,
                                "status": status or "예정", "categories": categories or []})
        return ("task 생성 승인 요청을 올렸습니다. 보스 ✅ 후 노션에 생성됩니다. 추가 시도 금지." if ts else "task 제안 게시 실패.")
    except Exception as e:
        return "task 제안 오류: %s" % str(e)[:140]


def _exec_tool(name, inp):
    inp = inp or {}
    if name == "local_read":
        return _local_read(inp.get("path", ""))
    if name == "recording_read":
        return _recording_read(inp.get("op", "list"), inp.get("arg", ""),
                               bool(inp.get("include_transcript", False)))
    if name == "slack_files":
        return _slack_files(inp.get("channel", ""))
    if name == "slack_file_read":
        return _run(["python3", os.path.join(BIN, "tory_slack_file.py"), inp.get("file_id", "")], cap=20000)
    if name == "propose_send":
        return _propose_send(inp.get("channel", ""), inp.get("text", ""), inp.get("label", ""), inp.get("thread"))
    if name == "propose_gmail":
        return _propose_gmail(inp.get("to", ""), inp.get("subject", ""), inp.get("body", ""))
    if name == "propose_calendar":
        return _propose_calendar(inp.get("summary", ""), inp.get("start", ""), inp.get("end", ""),
                                 inp.get("description", ""), inp.get("attendees"))
    if name == "propose_notion":
        return _propose_notion(inp.get("target_id", ""), inp.get("title", ""), inp.get("body", ""),
                               inp.get("mode", "create"))
    if name == "propose_notion_task":
        return _propose_notion_task(inp.get("title", ""), inp.get("priority"), inp.get("due"),
                                    inp.get("status"), inp.get("categories"))
    if name == "memory_read":
        scope = (inp.get("scope") or "company").lower()
        uid = "tory" if scope == "personal" else "awm_confidential"
        return _mem_search(inp.get("query", ""), uid)
    if name == "slack_read":
        return _run(["bash", os.path.join(BIN, "tory_slack_read.sh"), inp.get("method", ""), inp.get("query", "")])
    if name == "gmail_read":
        return _run(["python3", os.path.join(BIN, "tory_gmail_read.py"), inp.get("op", ""), inp.get("arg", "")])
    if name == "notion_read":
        return _run(["bash", os.path.join(BIN, "tory_notion_read.sh"), inp.get("op", ""), inp.get("arg", "")])
    if name == "drive_read":
        return _run(["python3", os.path.join(BIN, "tory_drive_read.py"), inp.get("op", ""), inp.get("arg", "")])
    return "알 수 없는 도구: %s" % name


def _json_object_after(text, start):
    """모델이 tool_calls 대신 `to=slack_read ... {}` 텍스트를 낸 경우 JSON 인자를 회수한다."""
    i = (text or "").find("{", start)
    if i < 0:
        return None, start
    try:
        obj, rel_end = json.JSONDecoder().raw_decode(text[i:])
    except Exception:
        return None, i + 1
    return obj, i + rel_end


def _text_tool_calls(text, allowed=None, max_calls=8):
    allowed = set(allowed or TOOL_NAMES)
    calls = []
    for m in TEXT_TOOL_RE.finditer(text or ""):
        name = m.group(1)
        if name not in allowed:
            continue
        args, _ = _json_object_after(text, m.end())
        if isinstance(args, dict):
            calls.append((name, args))
        if len(calls) >= max_calls:
            break
    return calls


def _looks_like_tool_leak(text):
    text = text or ""
    return bool(TEXT_TOOL_RE.search(text) or META_LEAK_RE.search(text))


def _run_text_tool_calls(calls):
    lines = ["[텍스트형 도구 호출 실행 결과]"]
    for name, args in calls:
        out = _exec_tool(name, args)
        arg_s = json.dumps(args, ensure_ascii=False, sort_keys=True)
        lines.append("%s(%s) =>\n%s" % (name, arg_s[:500], out[:6000]))
    lines.append("위 결과를 바탕으로 최종 답변만 한국어로 출력하라. `to=...` 도구 호출문, JSON, 내부 도구명은 절대 출력하지 마라.")
    return "\n\n".join(lines)[:18000]


def _is_retryable_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (400, 404, 429, 500, 502, 503, 504)
    return isinstance(e, urllib.error.URLError)


def _post_messages(messages, key, system, model, tries=2, include_tools=True):
    """:8321 messages 호출. 일시적 5xx/429·연결오류는 백오프 재시도(프록시 502 는 보통 일시적).
    include_tools=False 면 도구 없이 호출(루프 소진 시 최종 답변 합성 강제용)."""
    body = {"model": model, "max_tokens": 2200, "system": system, "messages": messages}
    if include_tools:
        body["tools"] = TOOLS
    data = json.dumps(body).encode()
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            BASE.rstrip("/") + "/v1/messages", data=data,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "anthropic-version": "2023-06-01"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    raise last


def _openai_tools():
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {"type": "object"})}}
            for t in TOOLS]


def _post_chat(messages, key, model, tries=2):
    """:8321 chat/completions 호출. GPT fallback 전용."""
    body = {"model": model, "messages": messages, "tools": _openai_tools(),
            "tool_choice": "auto", "max_tokens": 2200, "stream": False}
    data = json.dumps(body).encode()
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            BASE.rstrip("/") + "/v1/chat/completions", data=data,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    raise last


def _chat_text(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                parts.append(c.get("text") or c.get("content") or "")
        return "".join(parts).strip()
    return ""


def _run_chat_finalizer(system, user, key, model):
    """도구付き GPT 가 빈 응답을 줄 때 마지막으로 텍스트 답변만 강제한다."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system + "\n도구 호출 없이, 최종 답변 텍스트만 한국어로 출력하라. 빈 응답 금지. `to=slack_read` 같은 내부 도구 호출문은 절대 출력하지 마라."},
            {"role": "user", "content": user},
        ],
        "max_tokens": 1200,
        "stream": False,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE.rstrip("/") + "/v1/chat/completions", data=data,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    out = _chat_text(msg)
    return "" if _looks_like_tool_leak(out) else out


def _finalize_messages(system, messages, key, model):
    """도구 루프가 소진됐을 때, 도구 없이 '지금까지 수집분으로 최종 답변 완성'을 강제한다.
    (부분결과 문자열을 그대로 사용자에게 게시하던 문제 방지 — 2026-06-15)"""
    msgs = list(messages) + [{"role": "user", "content": (
        "도구 사용을 멈추고, 지금까지 수집한 정보만으로 최종 답변을 한국어로 지금 완성하라. "
        "요청한 결과물(예: 회신/승인 요청 초안)은 반드시 끝까지 작성하고, 확인 못 한 부분만 '(확인 필요)'로 표시하라. "
        "사고과정·메타설명(영어 'Now I have'/'Let me'/'Here is', 한국어 '충분한 근거를 확보'/'~작성합니다' 류 서두) "
        "없이 첫 줄부터 결과물만. 내부 도구 호출문·JSON 금지.")}]
    try:
        resp = _post_messages(msgs, key, system, model, include_tools=False)
        txt = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
        return "" if _looks_like_tool_leak(txt) else txt
    except Exception:
        return ""


def _run_messages_agent(system, user, key, model, max_iters):
    """Anthropic messages 형식: system+user → tool_use/tool_result 루프.
    마지막 회차는 도구 없이 호출해 합성을 유도하고, 그래도 못 끝내면 finalizer 로 강제."""
    messages = [{"role": "user", "content": user}]
    for i in range(max_iters):
        last = i == max_iters - 1
        resp = _post_messages(messages, key, system, model, include_tools=not last)
        blocks = resp.get("content", [])
        tus = [b for b in blocks if b.get("type") == "tool_use"] if not last else []
        if not tus:
            txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            if txt:
                return txt
            if last:
                break
            # 빈 응답: 한 번 더 합성 유도
            messages.append({"role": "user", "content": "최종 답변을 한국어로 작성하라."})
            continue
        messages.append({"role": "assistant", "content": blocks})
        results = []
        for tu in tus:
            out = _exec_tool(tu.get("name", ""), tu.get("input", {}))
            results.append({"type": "tool_result", "tool_use_id": tu.get("id"), "content": out})
        messages.append({"role": "user", "content": results})
    fin = _finalize_messages(system, messages, key, model)
    return fin or "(조사가 길어졌습니다 — 범위를 좁혀 다시 요청해 주세요.)"


def _run_chat_agent(system, user, key, model, max_iters):
    """OpenAI chat 형식: GPT fallback 용 tool_calls 루프."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    empty_seen = False
    text_tool_seen = 0
    for _ in range(max_iters):
        resp = _post_chat(messages, key, model)
        msg = ((resp.get("choices") or [{}])[0].get("message") or {})
        calls = msg.get("tool_calls") or []
        if not calls:
            text = _chat_text(msg)
            text_calls = _text_tool_calls(text)
            if text_calls:
                text_tool_seen += 1
                if text_tool_seen > 3:
                    return _run_chat_finalizer(system, user, key, model)
                messages.append({"role": "assistant", "content": "[내부 텍스트형 도구 호출 감지 — 사용자에게 출력하지 않음]"})
                messages.append({"role": "user", "content": _run_text_tool_calls(text_calls)})
                continue
            if _looks_like_tool_leak(text):
                text_tool_seen += 1
                if text_tool_seen > 3:
                    return _run_chat_finalizer(system, user, key, model)
                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "user", "content": "직전 응답은 내부 도구 호출문이라 사용자에게 보낼 수 없습니다. 도구 호출문을 출력하지 말고, 필요한 도구는 tool_calls 로만 호출한 뒤 최종 답변 텍스트만 한국어로 출력하세요."})
                continue
            if text:
                return text
            if empty_seen:
                return _run_chat_finalizer(system, user, key, model)
            empty_seen = True
            messages.append({"role": "assistant", "content": ""})
            messages.append({"role": "user", "content": "직전 응답이 비었습니다. 도구 호출이 필요하면 tool_calls 를 내고, 아니면 최종 답변을 텍스트로 반드시 출력하세요. 빈 응답은 실패입니다."})
            continue
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for tc in calls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            out = _exec_tool(fn.get("name", ""), args)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
    return "(도구 루프 최대치 도달 — 부분 결과만)"


def _is_chat_model(model):
    low = (model or "").lower()
    return low.startswith("gpt-")


def run_agent(system, user, key, max_iters=16):
    """claude -p 대체. Opus 실패(429/5xx/빈 응답) 시 GPT chat/completions 로 fallback."""
    models = [PRIMARY_MODEL] + [m for m in GPT_FALLBACK_MODELS if m != PRIMARY_MODEL]
    errors = []
    for model in models:
        try:
            if _is_chat_model(model):
                out = _run_chat_agent(system, user, key, model, max_iters)
            else:
                out = _run_messages_agent(system, user, key, model, max_iters)
            if (out or "").strip():
                if _looks_like_tool_leak(out):
                    errors.append("%s: tool-call text leaked" % model)
                    continue
                return out.strip()
            errors.append("%s: empty output" % model)
            continue
        except Exception as e:
            errors.append("%s: %s" % (model, str(e)[:180]))
            if not _is_retryable_error(e) and model == models[-1]:
                break
            continue
    raise RuntimeError("all agent models failed: " + " | ".join(errors))


# ──────────────────────────────────────────────────────────────────────────
# 분류기(triage) — 에이전트 팀의 1차 역할. 도구 없는 값싼 단발 호출로
#   (1) 응답할지(노이즈 거르기) (2) 처리 경로 (3) 다듬은 의도 를 정한다.
#   어떤 실패에도 {reply:True, route:"research"} 로 폴백 → 진짜 요청을 막지 않는다(기존 동작과 동일).
# ──────────────────────────────────────────────────────────────────────────
TRIAGE_MODEL = os.environ.get("TORY_TRIAGE_MODEL", GPT_FALLBACK_MODELS[0] if GPT_FALLBACK_MODELS else "gpt-5.4")

TRIAGE_SYSTEM = (
    "너는 %s(%s %s) 비서 '%s'의 1차 분류기다. 비서 채널/스레드에 들어온 "
    "보스의 한 메시지를 보고, %s가 응답해야 하는지와 처리 경로를 정한다. 조사는 하지 말고 분류만 하라.\n"
    % (BOSS_NAME, COMPANY_NAME, BOSS_TITLE, ASSISTANT_NAME, ASSISTANT_NAME) +
    "오직 JSON 한 줄만 출력: {\"reply\": true|false, \"route\": \"<경로>\", \"intent\": \"<한 줄 의도>\"}\n"
    "경로(route): noise | todo | employee | followup | research | simple\n"
    "- noise: 웃음(ㅋㅋ/ㅎㅎ)·이모지·인정(ㅇㅋ/넵/감사)·혼잣말·잡담 → reply=false\n"
    "- todo: '나 뭐해야 돼/할 일/우선순위' 류\n"
    "- employee: 특정 사람·직원 조회(누구/소속/담당/연락)\n"
    "- followup: 직전 브리핑 항목·번호('3번 더 자세히')·회신 초안에 대한 추가 지시/심화\n"
    "- research: 근거를 찾아 답할 일반 업무 질문/요청\n"
    "- simple: 조사 없이 바로 답할 인사·간단 확인\n"
    "기준: 명령·질문·요청·확인요구·번호지정은 reply=true. 웃음·감탄·고맙다·알겠다 류만 reply=false."
)


def _chat_once(system, user, key, model, max_tokens=320, timeout=45):
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
            "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(BASE.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return _chat_text(((resp.get("choices") or [{}])[0].get("message") or {}))


def _msg_once(system, user, key, model, max_tokens=320, timeout=70):
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(BASE.rstrip("/") + "/v1/messages", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                          "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()


def _parse_json_obj(s):
    s = s or ""
    i = s.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[i:])
        return obj
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def triage(text, context, key, model=None):
    """1차 분류. dict{reply,route,intent}. 어떤 오류든 안전 폴백(응답 진행)."""
    user = "[직전 맥락]\n%s\n\n[분류할 메시지]\n%s" % ((context or "(없음)")[:1500], (text or "")[:1500])
    for m in [model or TRIAGE_MODEL, PRIMARY_MODEL]:
        try:
            out = _chat_once(TRIAGE_SYSTEM, user, key, m) if _is_chat_model(m) \
                else _msg_once(TRIAGE_SYSTEM, user, key, m)
            d = _parse_json_obj(out)
            if isinstance(d, dict) and d.get("route"):
                d["reply"] = bool(d.get("reply", d.get("route") != "noise"))
                d["route"] = str(d.get("route"))
                d["intent"] = str(d.get("intent") or "")
                return d
        except Exception:
            continue
    return {"reply": True, "route": "research", "intent": "", "_fallback": True}


if __name__ == "__main__":
    import sys
    k = ""
    for l in open(os.path.expanduser("~/.hermes/.env")):
        if l.startswith("OPENAI_API_KEY="):
            k = l.split("=", 1)[1].strip().strip('"').strip("'"); break
    if len(sys.argv) > 1 and sys.argv[1] == "--triage":
        print(triage(sys.argv[2] if len(sys.argv) > 2 else "3번 더 자세히 봐줘", "", k))
    else:
        sysmsg = "너는 %s의 비서 '%s'다. 주어진 도구로 회사 데이터를 조사해 근거 있는 답을 한국어로 낸다." % (BOSS_NAME, ASSISTANT_NAME)
        q = sys.argv[1] if len(sys.argv) > 1 else "테스트: 노션에서 'ERP 알림' 검색해서 뭐가 있는지 한 줄로."
        print(run_agent(sysmsg, q, k))
