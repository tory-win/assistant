#!/usr/bin/env python3
"""
tory_format.py — 토리 출력 공용 가독성 렌더 (2026-06-13).

브리핑·문의응답(watcher)·조사보고(replier) 세 경로가 같은 함수로 Slack mrkdwn 을
정돈한다. LLM 출력의 흔한 깨짐을 결정적으로 흡수한다:
  - ## 제목 (슬랙은 헤딩 미지원) → *굵게*
  - [텍스트](url) 마크다운 링크 → <url|텍스트>
  - **두 별표**·__밑줄__ → *별표 하나*
  - 줄머리 -, *, + 불릿 → • (일관)
  - 빈 줄 3개 이상 → 1개
  - 소제목만 있고 내용이 없는 빈 섹션 → 통째로 제거

순수 문자열 변환이다 — 네트워크·상태·예외 없음(빈/None 입력 안전).
가독성 규칙(READ_CORE)은 세 PROMPT 가 import 해 LLM 단계부터 가독성을 유도하고,
render_slack 이 마지막에 강제 정돈한다(이중 안전망 · 단일 출처).

주의: 숫자 목록(`1.` `2.`)은 건드리지 않는다 — 브리핑 완료 리액션(1️⃣~🔟)이
항목 고유번호와 매칭되므로 번호를 바꾸면 안 된다.
"""
import re

# ── 세 경로 PROMPT 공통 가독성 코어(모바일 슬랙 기준). %s 등 % 문자를 쓰지 않는다
#    (PROMPT 템플릿이 % 포매팅을 쓰므로). 경로별 구조 지시와 함께 끼워 쓴다. ──
READ_CORE = (
    "[가독성 — 모바일 슬랙 기준, 반드시 지켜라]\n"
    "- 강조는 *별표 하나* 만. **두 개**·__밑줄__·# 제목·표 마크다운 금지(슬랙에서 깨진다).\n"
    "- 목록은 한 줄에 한 항목, 줄머리 기호는 • 하나. 한 항목은 한 호흡(대략 60자 이내) — 길면 끊어 핵심만 남겨라.\n"
    "- 링크는 <url|열기> 형태로. 인사·자기소개·같은 말 반복·군더더기는 빼고 정보만.\n"
)

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*$")
_BOLD2 = re.compile(r"\*\*(.+?)\*\*", re.S)
_UND2 = re.compile(r"(?<!\w)__(.+?)__(?!\w)", re.S)
_BULLET = re.compile(r"(?m)^([ \t]*)[-*+][ \t]+")
_MULTI_BLANK = re.compile(r"\n[ \t]*\n[ \t]*\n+")

_EMOJI_PREFIX = re.compile(r"^(?::[a-z0-9_+\-]+:\s*)+")


def _is_heading(s):
    """한 줄 전체가 *라벨* (앞에 :emoji: 가 붙어도 됨) 인 짧은 소제목인가."""
    s = s.strip()
    if not s:
        return False
    core = _EMOJI_PREFIX.sub("", s)
    return bool(re.fullmatch(r"\*[^*\n]{1,40}\*", core))


def _is_divider(s):
    s = s.strip()
    return bool(s) and bool(re.fullmatch(r"[─—\-_=·•~\s]{3,}", s))


def _strip_empty_sections(text):
    """*소제목* 만 있고 그 아래 실제 내용이 없는 섹션을 통째로 뺀다.
    소제목 다음의 첫 비어있지 않은 줄이 또 소제목이거나 구분선이거나 끝이면 내용 없는 섹션."""
    lines = text.split("\n")
    keep = [True] * len(lines)
    for i, ln in enumerate(lines):
        if not _is_heading(ln):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or _is_heading(lines[j]) or _is_divider(lines[j]):
            keep[i] = False
    return "\n".join(ln for ln, k in zip(lines, keep) if k)


# ── 노이즈 판정(2026-06-15): 웃음·이모지·리액션성 한마디엔 응답하지 않는다.
#    값싼 결정적 1차 게이트 — 명백한 노이즈는 LLM(triage) 호출 없이 즉시 거른다.
#    애매한 회색지대는 False 를 돌려 상위(triage)가 판단하게 둔다(과소차단 안전).
NOISE_WORDS = {
    "ㅇㅋ", "ㅋㅋ", "ㅎㅎ", "ㅋㅋㅋ", "ㄳ", "ㄱㅅ", "넵", "네", "넹", "예", "응", "웅", "ㅇㅇ", "ㅇ",
    "오케이", "오키", "오키도키", "감사", "감사합니다", "고마워", "고맙습니다", "고마워요", "땡큐",
    "좋아", "좋아요", "좋네", "굿", "나이스", "확인", "완료", "됐어", "됐다", "됨", "ok", "okay", "okk",
    "ㅎ", "ㅋ", "ㅠ", "ㅠㅠ", "ㅜㅜ", "오", "오오", "와", "우와", "헐", "음", "흠", "아", "아하",
    "그르네", "그렇네", "ㅇㅋㅇㅋ", "ㄿ", "ㅗㅋ", "ㅊㅋ", "축하", "수고", "고생", "굿굿",
}
_EMOJI_TOKEN = re.compile(r":[a-z0-9_+\-']+:")
_EMOJI_UNICODE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "←-⇿⌀-⏿⬀-⯿️‍⃣]"
)
# 잔여물 제거용: 공백·웃음/감탄 자모·문장부호·괄호·따옴표. 남는 게 없으면 순수 노이즈.
_RESIDUAL = re.compile(r"[\sㅋㅎㅠㅜㅡ‥…・·.,!?~^\-_'\"`()\[\]{}<>/\\|＋+*=@#$%&]+")
_BARE = re.compile(r"[\s.,!?~…・·^\-_'\"`]+")


def is_noise(text):
    """웃음·이모지·리액션성 한마디면 True(응답 안 함). 업무성 한 글자라도 있으면 False."""
    s = (text or "").strip()
    if not s:
        return True
    if _BARE.sub("", s.lower()) in NOISE_WORDS:
        return True
    t = _EMOJI_UNICODE.sub("", _EMOJI_TOKEN.sub("", s))
    t = _RESIDUAL.sub("", t)
    return not t


# ── 발췌 정리(2026-06-15): 멘션·채널·링크 markup 을 사람이 읽는 형태로, 앞쪽 멘션은 제거.
#    legend 가 18자에서 <@U…> markup 중간을 잘라 raw 가 보이고 실제 내용이 가려지던 문제 해결.
_MENTION_REF = re.compile(r"<@[UW][A-Z0-9]+(?:\|([^>]+))?>")
_CH_REF = re.compile(r"<#C[A-Z0-9]+(?:\|([^>]+))?>")
_URL_REF = re.compile(r"<(https?://[^>|]+)(?:\|([^>]+))?>")
_LEAD_MENTIONS = re.compile(r"^(?:@\S+\s+)+")


def clean_excerpt(text):
    """발췌를 사람이 읽을 수 있게 정리. <@U…|이름>→@이름, <#C…|채널>→#채널, <url|텍스트>→텍스트,
    앞쪽 멘션 제거(실제 내용이 앞으로). 내용이 전부 멘션이면 해석본 유지."""
    s = str(text or "")
    s = _MENTION_REF.sub(lambda m: "@" + ((m.group(1) or "").split(" - ")[0].strip() or "멘션"), s)
    s = _CH_REF.sub(lambda m: "#" + ((m.group(1) or "").strip() or "채널"), s)
    s = _URL_REF.sub(lambda m: (m.group(2) or "").strip(), s)
    s = re.sub(r"\s+", " ", s).strip()
    body = _LEAD_MENTIONS.sub("", s).strip()
    return body or s


def render_slack(text):
    """LLM 출력을 일관된 Slack mrkdwn 으로 정돈한다. 입력이 비면 빈 문자열."""
    if not text:
        return ""
    t = str(text)
    t = _MD_LINK.sub(r"<\2|\1>", t)   # [txt](url) → <url|txt>
    t = _HEADING.sub(r"*\1*", t)      # ## 제목 → *제목*
    t = _BOLD2.sub(r"*\1*", t)        # **굵게** → *굵게*
    t = _UND2.sub(r"*\1*", t)         # __굵게__ → *굵게*
    t = _BULLET.sub(r"\1• ", t)       # 줄머리 -,*,+ → •
    t = _strip_empty_sections(t)
    t = _MULTI_BLANK.sub("\n\n", t)   # 빈 줄 3+ → 1개
    t = "\n".join(ln.rstrip() for ln in t.split("\n"))
    return t.strip()


if __name__ == "__main__":
    import sys
    print(render_slack(sys.stdin.read()))
