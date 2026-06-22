#!/usr/bin/env python3
"""
tory_notion_write.py — 승인 게이트 하 Notion 쓰기 (2026-06-15).

페이지 생성(부모 아래) / 기존 페이지에 본문 추가. NOTION_TOKEN(integration 'tory-read',
쓰기 capability 활성 확인됨)으로 Notion REST 직접 호출. **send_gate 가 보스 ✅ 후에만** 부른다.

주의: 대상 페이지/부모가 integration 에 *공유(Connections)* 돼 있어야 쓰기가 된다(아니면 object_not_found).
stdlib only. 토큰 값은 출력하지 않는다.
"""
import json
import os
import re
import urllib.error
import urllib.request

try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

ENV = PROFILE.get("env_file") or os.path.expanduser("~/.hermes/.env")
VER = "2022-06-28"


def _token():
    for l in open(ENV):
        if l.startswith("NOTION_TOKEN="):
            return l.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _api(method, url, body, tok):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                 headers={"Authorization": "Bearer " + tok, "Notion-Version": VER,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            try:
                return r.status, json.load(r)
            except Exception:
                return r.status, {}
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok, "Notion-Version": VER})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def _object_kind(obj_id, tok):
    if not obj_id:
        return "missing"
    s, _ = _get("https://api.notion.com/v1/pages/%s" % obj_id, tok)
    if 200 <= s < 300:
        return "page"
    s, _ = _get("https://api.notion.com/v1/databases/%s" % obj_id, tok)
    if 200 <= s < 300:
        return "database"
    return "not_found"


_LINK_RE = re.compile(
    r"<(https?://[^>|]+)\|([^>\n]+)>"
    r"|\[([^\]\n]+)\]\((https?://[^)\s]+)\)"
    r"|(https?://[^\s<>()]+)"
)
_HEADING_RE = re.compile(r"^\s*(#{1,3})\s+(.+?)\s*$")
_BOLD_HEADING_RE = re.compile(r"^\s*\*([^*\n]{1,100})\*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•])\s+(.+?)\s*$")
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_TODO_RE = re.compile(r"^\s*(?:[-*+•]\s*)?(?:\[( |x|X)\]|☐|☑)\s+(.+?)\s*$")
_DIVIDER_RE = re.compile(r"^\s*[-_=]{3,}\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s+(.+?)\s*$")


def _text_chunks(text, n=1900):
    text = str(text or "")
    while len(text) > n:
        yield text[:n]
        text = text[n:]
    if text:
        yield text


def _add_rich(out, text, url=None):
    for chunk in _text_chunks(text):
        node = {"type": "text", "text": {"content": chunk}}
        if url:
            node["text"]["link"] = {"url": url}
        out.append(node)


def _rich_text(text):
    """Slack/Markdown/bare links -> Notion rich_text with clickable hrefs."""
    text = str(text or "")
    out = []
    pos = 0
    for m in _LINK_RE.finditer(text):
        if m.start() > pos:
            _add_rich(out, text[pos:m.start()])
        if m.group(1):
            url, label = m.group(1), m.group(2)
        elif m.group(4):
            url, label = m.group(4), m.group(3)
        else:
            raw = m.group(5)
            trail = ""
            while raw and raw[-1] in ".,)]}>":
                trail = raw[-1] + trail
                raw = raw[:-1]
            url, label = raw, raw
            _add_rich(out, label, url)
            if trail:
                _add_rich(out, trail)
            pos = m.end()
            continue
        _add_rich(out, label, url)
        pos = m.end()
    if pos < len(text):
        _add_rich(out, text[pos:])
    return out or [{"type": "text", "text": {"content": " "}}]


def _text_block(kind, text, **extra):
    payload = {"rich_text": _rich_text(text)}
    payload.update(extra)
    return {"object": "block", "type": kind, kind: payload}


def _paragraphs(text):
    text = str(text or "").strip()
    if not text:
        return []
    return [_text_block("paragraph", chunk) for chunk in _text_chunks(text, 1800)]


def _line_block(line):
    s = line.rstrip()
    if not s.strip():
        return None
    if _DIVIDER_RE.match(s):
        return {"object": "block", "type": "divider", "divider": {}}
    m = _HEADING_RE.match(s)
    if m:
        level = min(3, len(m.group(1)))
        return _text_block("heading_%d" % level, m.group(2).strip()[:1900])
    m = _BOLD_HEADING_RE.match(s)
    if m:
        return _text_block("heading_2", m.group(1).strip()[:1900])
    m = _TODO_RE.match(s)
    if m:
        checked = (m.group(1) or "").lower() == "x" or "☑" in s[:3]
        return _text_block("to_do", m.group(2).strip()[:1900], checked=checked)
    m = _BULLET_RE.match(s)
    if m:
        return _text_block("bulleted_list_item", m.group(1).strip()[:1900])
    m = _NUMBER_RE.match(s)
    if m:
        return _text_block("numbered_list_item", m.group(1).strip()[:1900])
    m = _QUOTE_RE.match(s)
    if m:
        return _text_block("quote", m.group(1).strip()[:1900])
    return None


def _text_to_blocks(body):
    out, para = [], []

    def flush_para():
        if para:
            out.extend(_paragraphs("\n".join(para)))
            para[:] = []

    for line in (body or "").split("\n"):
        if not line.strip():
            flush_para()
            continue
        block = _line_block(line)
        if block:
            flush_para()
            out.append(block)
        else:
            para.append(line.rstrip())
        if len(out) >= 90:
            break
    flush_para()
    return out[:90] or _paragraphs(" ")


def create_page(parent_id, title, body):
    """부모 페이지 아래 새 페이지 생성. (ok, url|err)."""
    tok = _token()
    if not (tok and parent_id):
        return False, "Notion 토큰/부모 id 필요"
    kind = _object_kind(parent_id, tok)
    if kind == "database":
        return False, "Notion 부모 id가 데이터베이스입니다. 보고서/문서 생성은 공유된 부모 페이지 id가 필요합니다."
    if kind != "page":
        return False, "Notion 부모 페이지 접근 불가(%s): integration 공유 또는 target_id 확인 필요" % kind
    payload = {"parent": {"page_id": parent_id},
               "properties": {"title": {"title": [{"text": {"content": (title or "제목 없음")[:200]}}]}},
               "children": _text_to_blocks(body)}
    s, r = _api("POST", "https://api.notion.com/v1/pages", payload, tok)
    if 200 <= s < 300:
        return True, r.get("url") or r.get("id") or "(생성됨)"
    return False, "Notion 페이지 생성 실패(%s): %s" % (s, (r.get("message") or "")[:140])


def append_blocks(page_id, title, body):
    """기존 페이지에 본문 추가(선택적 소제목 + 문단). (ok, info|err)."""
    tok = _token()
    if not (tok and page_id):
        return False, "Notion 토큰/페이지 id 필요"
    kind = _object_kind(page_id, tok)
    if kind != "page":
        return False, "Notion 페이지 접근 불가(%s): integration 공유 또는 target_id 확인 필요" % kind
    blocks = []
    if title:
        blocks.append(_text_block("heading_2", title[:200]))
    blocks += _text_to_blocks(body)
    s, r = _api("PATCH", "https://api.notion.com/v1/blocks/%s/children" % page_id, {"children": blocks}, tok)
    if 200 <= s < 300:
        return True, "추가 완료"
    return False, "Notion 추가 실패(%s): %s" % (s, (r.get("message") or "")[:140])


if __name__ == "__main__":
    import sys
    print(_token() and "token present" or "no token")
