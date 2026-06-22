#!/usr/bin/env python3
"""
tory_notion_tasks.py — 오승현 담당 노션 task 를 브리핑 '할 일'에 넣기 (2026-06-15).

사용자 지시(2026-06-15): 브리핑이 슬랙·Gmail 뿐 아니라 노션의 *내 task*(담당자=오승현)도 보게 한다.
대상 DB(전사 업무 DB)에서 담당자=오승현 AND 완료=false 인 항목을 마감일 오름차순으로 가져온다.

읽기 전용. NOTION_TOKEN(integration 'tory-read', 이 DB 에 공유됨)으로 REST 직접 호출.
best-effort — 실패해도 [] 반환(브리핑은 정상 진행). state/notion-attention.json 에도 캐시(워처가 '나 뭐해야 돼' 답에 사용).
"""
import datetime
import json
import os
import time
import urllib.error
import urllib.request

try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

ENV = PROFILE.get("env_file") or os.path.expanduser("~/.hermes/.env")
STATE_DIR = PROFILE.get("state_dir") or os.path.expanduser("~/.torymemory/state")
STATE = os.path.join(STATE_DIR, "notion-attention.json")
BOSS_NAME = PROFILE.get("boss_name") or "오승현"
IS_DEFAULT_PROFILE = (PROFILE.get("id") or "tory") == "tory"
DB_ID = PROFILE.get("notion_task_db_id") or ("17eea3ff-6c9b-8102-b0cd-dd7ee2c52ee7" if IS_DEFAULT_PROFILE else "")
OWNER_ID = PROFILE.get("notion_task_owner_id") or ("181ea3ff-6c9b-802c-aaa1-d8ffba796d18" if IS_DEFAULT_PROFILE else "")
OWNER_NAME = PROFILE.get("notion_task_owner_name") or BOSS_NAME
VER = "2022-06-28"


def _token():
    try:
        for l in open(ENV):
            if l.startswith("NOTION_TOKEN="):
                return l.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _api(method, url, body, tok):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + tok, "Notion-Version": VER,
                                          "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except Exception:
        return {}


def _title_of(page):
    for v in (page.get("properties") or {}).values():
        if isinstance(v, dict) and v.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in (v.get("title") or [])).strip()
    return ""


_DB_CACHE = None


def _database(tok):
    global _DB_CACHE
    if _DB_CACHE is None:
        _DB_CACHE = _api("GET", "https://api.notion.com/v1/databases/%s" % DB_ID, None, tok) if DB_ID else {}
    return _DB_CACHE if _DB_CACHE.get("object") == "database" else {}


def _first_prop(db, types=(), names=()):
    props = db.get("properties") or {}
    type_set = set(types)
    for name in names:
        prop = props.get(name)
        if isinstance(prop, dict) and (not type_set or prop.get("type") in type_set):
            return name
    for name, prop in props.items():
        if isinstance(prop, dict) and prop.get("type") in type_set:
            return name
    return ""


def _prop_name(prop_value):
    if not isinstance(prop_value, dict):
        return None
    typ = prop_value.get("type")
    if typ == "select":
        return ((prop_value.get("select") or {}).get("name"))
    if typ == "status":
        return ((prop_value.get("status") or {}).get("name"))
    return None


def _option_value(prop, wanted, aliases):
    typ = prop.get("type")
    opts = [o.get("name") for o in ((prop.get(typ) or {}).get("options") or []) if o.get("name")]
    wanted = str(wanted or "").strip()
    candidates = [wanted] + aliases.get(wanted, [])
    compact = {o.replace(" ", ""): o for o in opts}
    for cand in candidates:
        if not cand:
            continue
        if cand in opts:
            return cand
        if cand.replace(" ", "") in compact:
            return compact[cand.replace(" ", "")]
    return ""


def _resolve_notion_user_id(tok):
    if OWNER_ID:
        return OWNER_ID
    if not OWNER_NAME:
        return ""
    cursor = ""
    exact = ""
    contains = ""
    while True:
        qs = "?page_size=100" + (("&start_cursor=%s" % cursor) if cursor else "")
        d = _api("GET", "https://api.notion.com/v1/users%s" % qs, None, tok)
        for user in d.get("results") or []:
            name = user.get("name") or ""
            if name.replace(" ", "") == OWNER_NAME.replace(" ", ""):
                exact = user.get("id") or ""
            elif OWNER_NAME in name and not contains:
                contains = user.get("id") or ""
        cursor = d.get("next_cursor") or ""
        if exact or not (d.get("has_more") and cursor):
            break
    return exact or contains


def _resolve_relation_owner_id(tok, rel_db):
    if OWNER_ID:
        return OWNER_ID
    if not (tok and rel_db and OWNER_NAME):
        return ""
    people_db = _api("GET", "https://api.notion.com/v1/databases/%s" % rel_db, None, tok)
    title_prop = _first_prop(people_db, ("title",))
    filters = []
    if title_prop:
        filters.append({"property": title_prop, "title": {"equals": OWNER_NAME}})
        filters.append({"property": title_prop, "title": {"contains": OWNER_NAME}})
    for flt in filters:
        d = _api("POST", "https://api.notion.com/v1/databases/%s/query" % rel_db,
                 {"filter": flt, "page_size": 10}, tok)
        for page in d.get("results") or []:
            if _title_of(page).replace(" ", "") == OWNER_NAME.replace(" ", "") or OWNER_NAME in _title_of(page):
                return page.get("id") or ""
    return ""


def _resolve_owner(tok, db=None):
    if not (tok and DB_ID and OWNER_NAME):
        return "", "", ""
    db = db or _database(tok)
    props = db.get("properties") or {}
    owner_name = "담당자" if isinstance(props.get("담당자"), dict) else ""
    if not owner_name:
        owner_name = _first_prop(db, ("people", "relation"))
    prop = props.get(owner_name) or {}
    typ = prop.get("type")
    if typ == "people":
        return owner_name, typ, _resolve_notion_user_id(tok)
    if typ == "relation":
        rel_db = ((prop.get("relation") or {}).get("database_id") or "")
        return owner_name, typ, _resolve_relation_owner_id(tok, rel_db)
    return "", "", ""


def _resolve_owner_id(tok):
    return _resolve_owner(tok)[2]


def open_tasks(limit=50):
    """담당자=<profile boss> AND 완료=false AND **우선순위 있는** task만. 우선순위순(스키마 옵션순)→마감순.
    '0(자연스럽게 완료 처리 된 것)'은 제외(사실상 완료). [{title,status,due,priority,url,overdue}]."""
    tok = _token()
    db = _database(tok)
    owner_prop, owner_type, owner_id = _resolve_owner(tok, db)
    if not (tok and DB_ID and db and owner_prop and owner_type and owner_id):
        return []
    owner_filter = {"property": owner_prop, owner_type: {"contains": owner_id}}
    filters = [owner_filter]
    complete_prop = _first_prop(db, ("checkbox",), ("완료",))
    if complete_prop:
        filters.append({"property": complete_prop, "checkbox": {"equals": False}})
    priority_prop = _first_prop(db, ("select",), ("우선순위",))
    if priority_prop:
        filters.append({"property": priority_prop, "select": {"is_not_empty": True}})
    due_prop = _first_prop(db, ("date",), ("마감일", "Due", "기한"))
    sorts = []
    if priority_prop:
        sorts.append({"property": priority_prop, "direction": "ascending"})
    if due_prop:
        sorts.append({"property": due_prop, "direction": "ascending"})
    flt = {"filter": {"and": filters}, "page_size": limit}
    if sorts:
        flt["sorts"] = sorts
    d = _api("POST", "https://api.notion.com/v1/databases/%s/query" % DB_ID, flt, tok)
    today = datetime.date.today().isoformat()
    out = []
    status_prop = _first_prop(db, ("select", "status"), ("상태", "진행상태", "Completed?"))
    for r in d.get("results") or []:
        p = r.get("properties") or {}
        title = "".join(x.get("plain_text", "") for k, v in p.items()
                        if isinstance(v, dict) and v.get("type") == "title" for x in (v.get("title") or []))
        if not title:
            continue
        pri = ((p.get(priority_prop) or {}).get("select") or {}).get("name") if priority_prop else None
        if pri and pri.startswith("0("):   # 자동완료 처리분 제외
            continue
        status = _prop_name(p.get(status_prop)) if status_prop else None
        due = ((p.get(due_prop) or {}).get("date") or {}).get("start") if due_prop else None
        out.append({"title": title, "status": status, "due": due, "priority": pri,
                    "url": r.get("url"), "overdue": bool(due and due < today)})
    return out


PRIORITY_OPTIONS = ["상단 고정용", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "회의"]
STATUS_OPTIONS = ["예정", "진행중", "완료"]


def create_task(title, priority=None, due=None, status="예정", categories=None):
    """프로필 담당자로 이 DB에 task 생성. (ok, url|err). priority/status 는 위 옵션 중 하나."""
    tok = _token()
    db = _database(tok)
    owner_prop, owner_type, owner_id = _resolve_owner(tok, db)
    title_prop = _first_prop(db, ("title",), ("업무 이름", "업무명", "Name"))
    if not (tok and DB_ID and db and title_prop and owner_prop and owner_type and owner_id and (title or "").strip()):
        return False, "제목/토큰/DB/담당자 id 필요"
    props = {
        title_prop: {"title": [{"text": {"content": title[:200]}}]},
    }
    if owner_type == "people":
        props[owner_prop] = {"people": [{"id": owner_id}]}
    else:
        props[owner_prop] = {"relation": [{"id": owner_id}]}

    complete_prop = _first_prop(db, ("checkbox",), ("완료",))
    if complete_prop:
        props[complete_prop] = {"checkbox": False}

    status_prop = _first_prop(db, ("select", "status"), ("상태", "진행상태", "Completed?"))
    if status_prop:
        prop = (db.get("properties") or {}).get(status_prop) or {}
        mapped = _option_value(prop, status, {
            "예정": ["업무대기/요청", "시작 전", "대기", "To Do"],
            "진행중": ["진행 중", "업무 진행중", "In progress"],
            "완료": ["업무 완료", "프로젝트/백로그 완료", "Done"],
        })
        if mapped:
            props[status_prop] = {prop.get("type"): {"name": mapped}}

    priority_prop = _first_prop(db, ("select",), ("우선순위",))
    if priority and priority_prop and str(priority) in PRIORITY_OPTIONS:
        props[priority_prop] = {"select": {"name": str(priority)}}

    due_prop = _first_prop(db, ("date",), ("마감일", "Due", "기한"))
    if due and due_prop:
        props[due_prop] = {"date": {"start": due}}

    category_prop = _first_prop(db, ("multi_select",), ("분류", "카테고리"))
    if categories and category_prop:
        prop = (db.get("properties") or {}).get(category_prop) or {}
        opts = {o.get("name") for o in ((prop.get("multi_select") or {}).get("options") or []) if o.get("name")}
        names = [c for c in categories if not opts or c in opts]
        if names:
            props[category_prop] = {"multi_select": [{"name": c} for c in names]}
    d = _api("POST", "https://api.notion.com/v1/pages",
             {"parent": {"database_id": DB_ID}, "properties": props}, tok)
    if d.get("object") == "page":
        return True, d.get("url") or d.get("id")
    return False, "task 생성 실패: %s" % (d.get("message") or d.get("code") or "unknown")


def refresh():
    """조회 후 캐시 파일에 기록(워처가 읽음). 반환=task 리스트."""
    tasks = open_tasks()
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"items": tasks, "_ts": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass
    return tasks


if __name__ == "__main__":
    t = open_tasks()
    print("%s 미완료 노션 task:" % BOSS_NAME, len(t))
    for x in t[:10]:
        print(" -", x["title"][:40], "| 마감", x["due"], "| overdue", x["overdue"])
