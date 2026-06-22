#!/usr/bin/env python3
"""
tory_blocks.py — 토리 답변 텍스트 → Slack Block Kit (2026-06-15).

문의응답·조사보고를 한 덩어리 mrkdwn 대신 헤더/섹션/구분선/컨텍스트 블록으로 쪼개
모바일에서 훑어보기 좋게 만든다. 입력은 render_slack 으로 정돈된 mrkdwn 가정.

계약: to_blocks(text) → (blocks, fallback_text). blocks 가 비거나 규격 위반이면 호출부가
text 로 폴백한다(항상 chat.postMessage 의 text 인자도 함께 보낸다 → 알림·접근성 보존).
순수 문자열 변환, 네트워크 없음. stdlib only.
"""
import re

_MRKDWN_LINK = re.compile(r"<(https?://[^>|]+)\|([^>]+)>")
_EMOJI_PREFIX = re.compile(r"^(?::[a-z0-9_+\-]+:\s*)+")
_HEAD = re.compile(r"^\s*\*([^*\n]{1,80})\*\s*$")

HEADER_MAX = 150
SECTION_MAX = 2900
MAX_BLOCKS = 45


def _plain(s):
    """헤더 블록은 plain_text 만 — mrkdwn 마크업 제거."""
    s = _MRKDWN_LINK.sub(r"\2", s or "")
    s = re.sub(r"[*_`~]", "", s)
    return s.strip()


def _section(md):
    return {"type": "section", "text": {"type": "mrkdwn", "text": md[:SECTION_MAX]}}


def _chunks(s, n=SECTION_MAX):
    s = s.rstrip()
    while len(s) > n:
        cut = s.rfind("\n", 0, n)
        if cut <= 0:
            cut = n
        yield s[:cut]
        s = s[cut:].lstrip("\n")
    if s:
        yield s


def to_blocks(text, footer=None, header=None):
    """rendered mrkdwn → (blocks, fallback_text). 실패 안전: 이상하면 ([], text).
    header 를 주면 그 줄을 헤더 블록으로 쓰고 text 전체를 본문으로 쪼갠다(브리핑 제목 고정용).
    header 가 없으면 첫 줄(결론)을 헤더로 뽑는다(문의응답·조사보고용)."""
    text = (text or "").strip()
    if not text and not header:
        return [], ""
    blocks = []
    if header:
        ht = _plain(header)[:HEADER_MAX]
        if ht:
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": ht, "emoji": True}})
        rest = text
    else:
        lines = text.split("\n")
        lead_idx = next((i for i, l in enumerate(lines) if l.strip()), 0)
        lead = _EMOJI_PREFIX.sub("", lines[lead_idx].strip())
        head_txt = _plain(lead)[:HEADER_MAX]
        if head_txt:
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": head_txt, "emoji": True}})
        rest = "\n".join(lines[lead_idx + 1:]).strip()
    # 2) 본문을 *소제목* 기준으로 끊어 섹션+구분선
    if rest:
        buf = []

        def flush():
            md = "\n".join(buf).strip()
            buf.clear()
            if md:
                for ch in _chunks(md):
                    if len(blocks) < MAX_BLOCKS:
                        blocks.append(_section(ch))

        for ln in rest.split("\n"):
            if _HEAD.match(ln):
                flush()
                if len(blocks) < MAX_BLOCKS:
                    blocks.append({"type": "divider"})
                    blocks.append(_section("*%s*" % _HEAD.match(ln).group(1)))
            else:
                buf.append(ln)
        flush()
    # 3) 푸터(출처/안내) → 컨텍스트 블록
    if footer and len(blocks) < MAX_BLOCKS:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:SECTION_MAX]}]})
    # 규격 안전장치: 블록이 1개(헤더만)면 본문 섹션을 추가
    if len(blocks) <= 1 and rest:
        blocks.append(_section(rest[:SECTION_MAX]))
    return blocks[:MAX_BLOCKS], (text or _plain(header or ""))


if __name__ == "__main__":
    import json
    import sys
    b, t = to_blocks(sys.stdin.read(), footer="토리 · 4소스 교차조사")
    print(json.dumps(b, ensure_ascii=False, indent=1))
