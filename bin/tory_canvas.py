#!/usr/bin/env python3
"""
tory_canvas.py — 토리 데일리 보드(독립 캔버스) 갱신 (2026-06-15).

브리핑 메시지(완료 리액션 매칭의 정본)는 그대로 두고, 가독성 높은 '오늘 보드'를 캔버스로
동반 제공하고 링크를 브리핑 메시지에 단다. 메시지는 알림+리액션, 캔버스는 정독용 — 역할 분리.

설계 결정(워크스페이스 API 제약을 직접 확인하고 고른 방식):
  - **독립(standalone) 캔버스**를 쓴다. 채널 탭 캔버스(conversations.canvases.create)는 호출마다
    탭이 누적되고 파일 삭제 후에도 탭이 남으며(검증), 제거 API 가 user 토큰 스코프 밖이라 잔여물이 쌓인다.
    독립 캔버스는 탭을 만들지 않아(검증: 생성 전후 채널 탭 수 불변) 잔여물이 없다.
  - 캔버스는 **부분 클리어가 불가**(섹션 lookup 이 헤더 타입만, 문단은 남음 — 검증)하므로,
    새로고침은 **이전 캔버스 통삭제 + 새로 생성**(stored id). 독립 캔버스 삭제는 깨끗하다.
  - canvases:write 는 **user 토큰**에만 있다. 캔버스는 문서이지 메시지 위장이 아니므로
    발송 게이트(타인 자동전송 금지)와 무관 — owner(win@)가 링크로 연다.

부작용: 매 갱신마다 직전 보드를 지우므로 과거 브리핑 메시지의 '보드 열기' 링크는 죽는다(현재 보드만 유효).
이는 의도된 트레이드오프(보드는 '오늘' 스냅샷). 모든 함수 best-effort — 실패 시 (None,None),
브리핑은 캔버스 없이 메시지만으로 정상 동작한다(캔버스는 순수 부가). stdlib only.
"""
import json
import os
import urllib.parse
import urllib.request

SLACK_API = "https://slack.com/api/"
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

STATE_DIR = PROFILE.get("state_dir") or os.path.join(os.path.expanduser("~"), ".torymemory", "state")
STATE_FILE = os.path.join(STATE_DIR, "canvas-board.json")
ASSISTANT_NAME = PROFILE.get("assistant_name") or "토리"


def _get(method, token, params, timeout=15):
    try:
        req = urllib.request.Request(
            SLACK_API + method + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return {"ok": False, "error": "neterr"}


def _post_json(method, token, payload, timeout=20):
    try:
        req = urllib.request.Request(
            SLACK_API + method, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return {"ok": False, "error": "neterr"}


def _read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(obj):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def workspace_meta(token):
    """team_id, workspace_url 을 auth.test 한 번으로. 실패해도 링크 없이 진행."""
    a = _get("auth.test", token, {})
    if not a.get("ok"):
        return None, None
    return a.get("team_id"), (a.get("url") or "").rstrip("/")


def canvas_url(workspace_url, team_id, canvas_id):
    if not (workspace_url and team_id and canvas_id):
        return None
    return "%s/docs/%s/%s" % (workspace_url, team_id, canvas_id)


def refresh_channel_board(user_token, channel_id, markdown, team_id=None, workspace_url=None):
    """독립 캔버스 보드를 통째로 새로고침한다(이전 통삭제+새로 생성). (url, canvas_id) 또는 (None, None).

    channel_id 는 저장 키로만 쓴다(여러 채널 보드 분리). markdown 은 Slack 캔버스 마크다운.
    """
    if not (user_token and (markdown or "").strip()):
        return None, None
    if not team_id or workspace_url is None:
        team_id, workspace_url = workspace_meta(user_token)
    st = _read_state()
    key = channel_id or "default"
    prev = (st.get(key) or {}).get("canvas_id")
    # 1) 직전 보드 통삭제(있으면) — 독립 캔버스라 탭 잔여물 없이 깨끗하다
    if prev:
        _post_json("canvases.delete", user_token, {"canvas_id": prev})
    # 2) 새 보드 생성
    r = _post_json("canvases.create", user_token,
                   {"title": "🗂️ %s 데일리 보드" % ASSISTANT_NAME,
                    "document_content": {"type": "markdown", "markdown": markdown[:90000]}})
    if not r.get("ok") or not r.get("canvas_id"):
        # 생성 실패 시 stored id 는 지운다(죽은 링크 방지)
        if prev:
            st.pop(key, None)
            _write_state(st)
        return None, None
    cid = r["canvas_id"]
    st[key] = {"canvas_id": cid}
    _write_state(st)
    return canvas_url(workspace_url, team_id, cid), cid


if __name__ == "__main__":
    import sys
    tok = ""
    env_file = PROFILE.get("env_file") or os.path.expanduser("~/.hermes/.env")
    for l in open(env_file):
        if l.startswith("SLACK_USER_TOKEN="):
            tok = l.split("=", 1)[1].strip().strip('"').strip("'")
            break
    ch = sys.argv[1] if len(sys.argv) > 1 else (PROFILE.get("assistant_channel_id") or "C0B997W7KGS")
    md = "# 🗂️ %s 데일리 보드\n_점검용 더미_\n\n## 지금 답할 것\n- 항목 예시\n" % ASSISTANT_NAME
    print(refresh_channel_board(tok, ch, md))
