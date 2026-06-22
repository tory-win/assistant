#!/usr/bin/env python3
"""
tory_send_gate.py — 승인 게이트 하의 실행(발송) 비서 (2026-06-15).

토리가 '이 내용을 #채널/상대에게 보낼까요?'를 비서 채널에 제안하고 ✅/❌ 리액션을 붙인다.
오승현(보스)이 ✅ 하면 그때서야 토리가 그 내용을 **원래 타깃에 보스 명의(user 토큰)로** 발송한다.
보스 승인 없이는 절대 타인에게 나가지 않는다 — 발송 게이트의 핵심 불변식을 코드로 강제.

  propose(...)  : 발송 제안 게시 + 대기 등록(state/send-gate.json)
  process(env)  : 대기 건의 보스 리액션을 확인 → ✅ 발송 / ❌ 취소 (워처 루프가 매 사이클 호출)

설계:
  - 제안 메시지는 비서 채널에만(타인에게 안 감). 승인 시 발송은 user 토큰 = 보스 명의 → 보스의
    답장으로 자연스럽게 나간다(토리 봇 위장 아님).
  - 멱등: 한 제안 = 한 발송. status 로 재발송 차단. 보스 리액션만 인정(타인 무시).
  - TTL 7일. 미승인은 그냥 만료(자동 발송 절대 없음).
stdlib only. 발송 토큰/대상은 호출부가 명시. 토큰 값은 출력하지 않는다.
"""
import json
import os
import time

import tory_command_watcher as w   # slack_call, CHANNEL, BOSS, load_env, _read_json, _write_json, react

STATE = os.path.join(w.STATE_DIR, "send-gate.json")
OK_EMO = "white_check_mark"
NO_EMO = "x"
TTL = 7 * 86400
ASSISTANT_NAME = getattr(w, "ASSISTANT_NAME", "토리")
ASSISTANT_ICON = getattr(w, "ASSISTANT_ICON", ":outbox_tray:")


def _load():
    return w._read_json(STATE, {})


def _save(d):
    w._write_json(STATE, d)


def propose(bot_token, target_channel, text, label, target_thread=None):
    """발송 제안을 비서 채널에 올리고 ✅/❌ 부착 + 대기 등록. proposal ts 반환(실패 시 None).
    target_channel/target_thread = 승인 시 실제 보낼 곳(원문 채널/스레드). text = 보낼 내용."""
    text = (text or "").strip()
    if not (bot_token and target_channel and text):
        return None
    body = ("📤 *발송 승인 요청* — %s\n승인(✅)하시면 제가 *원문에 당신 이름으로* 보냅니다. ❌ 면 취소.\n```%s```"
            % (label or target_channel, text[:1500]))
    r = w.slack_call("chat.postMessage", bot_token,
                     {"channel": w.CHANNEL, "text": body, "username": ASSISTANT_NAME,
                      "icon_emoji": ":outbox_tray:", "unfurl_links": "false"}, post=True)
    if not r.get("ok"):
        return None
    ts = r.get("ts")
    w.slack_call("reactions.add", bot_token, {"channel": w.CHANNEL, "timestamp": ts, "name": OK_EMO}, post=True)
    w.slack_call("reactions.add", bot_token, {"channel": w.CHANNEL, "timestamp": ts, "name": NO_EMO}, post=True)
    d = _load()
    d[ts] = {"target_channel": target_channel, "target_thread": target_thread,
             "text": text, "label": label, "status": "pending", "_t": time.time()}
    _save(d)
    w.log("send-gate proposed:", label, "->", target_channel)
    return ts


def propose_action(bot_token, label, preview, action):
    """슬랙 발송 외 실행(Gmail 초안·캘린더 일정 등) 승인 제안. action=dict{type,...}. ts 반환."""
    if not (bot_token and action):
        return None
    body = ("📤 *실행 승인 요청* — %s\n승인(✅)하시면 제가 실행합니다. ❌ 면 취소.\n%s"
            % (label or action.get("type"), (preview or "")[:1500]))
    r = w.slack_call("chat.postMessage", bot_token,
                     {"channel": w.CHANNEL, "text": body, "username": ASSISTANT_NAME,
                      "icon_emoji": ":outbox_tray:", "unfurl_links": "false"}, post=True)
    if not r.get("ok"):
        return None
    ts = r.get("ts")
    w.slack_call("reactions.add", bot_token, {"channel": w.CHANNEL, "timestamp": ts, "name": OK_EMO}, post=True)
    w.slack_call("reactions.add", bot_token, {"channel": w.CHANNEL, "timestamp": ts, "name": NO_EMO}, post=True)
    d = _load()
    d[ts] = {"action": action, "label": label, "status": "pending", "_t": time.time()}
    _save(d)
    w.log("send-gate proposed action:", action.get("type"), label)
    return ts


def _execute_action(action):
    """보스 ✅ 후 실제 실행. (ok, info). Gmail 은 초안만, 캘린더는 일정 등록."""
    typ = (action or {}).get("type")
    try:
        if typ in ("gmail_draft", "calendar_event"):
            import tory_google_write as gw
            if typ == "gmail_draft":
                ok, info = gw.gmail_draft(action.get("to", ""), action.get("subject", ""), action.get("body", ""))
                return ok, ("Gmail 초안을 만들었습니다 — Gmail에서 확인 후 발송하세요." if ok else info)
            ok, info = gw.calendar_event(action.get("summary", ""), action.get("start", ""),
                                         action.get("end", ""), action.get("description", ""),
                                         action.get("attendees"))
            return ok, (("일정 등록 완료 <%s|열기>" % info) if ok and str(info).startswith("http") else info)
        if typ in ("notion_page", "notion_append"):
            import tory_notion_write as nw
            if typ == "notion_page":
                ok, info = nw.create_page(action.get("parent_id", ""), action.get("title", ""), action.get("body", ""))
                return ok, (("노션 페이지 생성됨 <%s|열기>" % info) if ok and str(info).startswith("http") else info)
            ok, info = nw.append_blocks(action.get("page_id", ""), action.get("title", ""), action.get("body", ""))
            return ok, ("노션 페이지에 추가했습니다." if ok else info)
        if typ == "notion_task":
            import tory_notion_tasks as nt
            ok, info = nt.create_task(action.get("title", ""), action.get("priority"), action.get("due"),
                                      action.get("status") or "예정", action.get("categories"))
            return ok, (("노션 task 생성됨 <%s|열기>" % info) if ok and str(info).startswith("http") else info)
        return False, "알 수 없는 실행 유형: %s" % typ
    except Exception as e:
        return False, "실행 오류: %s" % str(e)[:140]


def _get_reactions(token, ts):
    """제안 메시지의 리액션을 conversations.history 로 읽는다(리액션 필드 동봉).
    reactions.get 는 reactions:read 스코프가 없어 못 쓴다 — history 는 groups:history 로 가능."""
    h = w.slack_call("conversations.history", token,
                     {"channel": w.CHANNEL, "latest": ts, "inclusive": "true", "limit": 1})
    msgs = h.get("messages") or []
    if msgs and msgs[0].get("ts") == ts:
        return msgs[0].get("reactions") or []
    return []


def _boss_reacted(reactions, name):
    for rx in reactions or []:
        if (rx.get("name") or "").split("::")[0] == name and w.BOSS in (rx.get("users") or []):
            return True
    return False


def process(env):
    """대기 발송 건의 보스 리액션을 확인해 ✅ 발송 / ❌ 취소. 워처 루프가 매 사이클 호출."""
    d = _load()
    if not d:
        return
    bot = env.get("SLACK_BOT_TOKEN", "").strip()
    utok = env.get("SLACK_USER_TOKEN", "").strip()
    if not utok:
        return
    changed = False
    now = time.time()
    for ts, g in list(d.items()):
        if g.get("status") != "pending":
            if now - g.get("_t", 0) > TTL:
                del d[ts]; changed = True
            continue
        if now - g.get("_t", 0) > TTL:
            g["status"] = "expired"; changed = True
            continue
        rxns = _get_reactions(utok, ts)
        if _boss_reacted(rxns, NO_EMO):
            g["status"] = "cancelled"; g["_t"] = now; changed = True
            w.slack_call("chat.postMessage", bot, {"channel": w.CHANNEL, "thread_ts": ts,
                         "text": "취소했습니다 — 발송하지 않았습니다.", "username": ASSISTANT_NAME,
                         "icon_emoji": ASSISTANT_ICON}, post=True)
            continue
        if not _boss_reacted(rxns, OK_EMO):
            continue
        # 승인됨 — 액션형(Gmail 초안·캘린더)이면 실행, 아니면 슬랙 발송
        if g.get("action"):
            ok, info = _execute_action(g["action"])
            if ok:
                g["status"] = "done"; g["_t"] = now; changed = True
                w.slack_call("chat.postMessage", bot, {"channel": w.CHANNEL, "thread_ts": ts,
                             "text": "✅ 처리했습니다 — *%s*\n%s" % (g.get("label") or g["action"].get("type"), info),
                             "username": ASSISTANT_NAME, "icon_emoji": ASSISTANT_ICON, "unfurl_links": "false"}, post=True)
            else:
                g["_attempts"] = int(g.get("_attempts", 0)) + 1; changed = True
                nonretry = g["action"].get("type", "").startswith("notion_") and (
                    "데이터베이스" in info or "접근 불가" in info or "(404)" in info or "object_not_found" in info
                )
                if nonretry or g["_attempts"] >= 3:
                    g["status"] = "failed"
                    w.slack_call("chat.postMessage", bot, {"channel": w.CHANNEL, "thread_ts": ts,
                                 "text": "처리 실패 — %s" % info, "username": ASSISTANT_NAME, "icon_emoji": ":warning:"}, post=True)
            continue
        # 슬랙 발송 → 보스 명의(user 토큰)로 원문에 발송
        params = {"channel": g["target_channel"], "text": g["text"][:w.MAX_REPLY],
                  "unfurl_links": "false", "unfurl_media": "false"}
        if g.get("target_thread"):
            params["thread_ts"] = g["target_thread"]
        sr = w.slack_call("chat.postMessage", utok, params, post=True)
        if sr.get("ok"):
            g["status"] = "sent"; g["_t"] = now; g["sent_ts"] = sr.get("ts"); changed = True
            link = w.slack_call("chat.getPermalink", utok,
                                {"channel": g["target_channel"], "message_ts": sr.get("ts")}).get("permalink", "")
            w.slack_call("chat.postMessage", bot, {"channel": w.CHANNEL, "thread_ts": ts,
                         "text": "✅ 보냈습니다 — *%s*%s" % (g.get("label") or g["target_channel"],
                                 (" <%s|열기>" % link) if link else ""),
                         "username": ASSISTANT_NAME, "icon_emoji": ASSISTANT_ICON, "unfurl_links": "false"}, post=True)
            w.log("send-gate SENT:", g.get("label"), "->", g["target_channel"])
        else:
            g["_attempts"] = int(g.get("_attempts", 0)) + 1
            if g["_attempts"] >= 5:
                g["status"] = "failed"; changed = True
                w.slack_call("chat.postMessage", bot, {"channel": w.CHANNEL, "thread_ts": ts,
                             "text": "발송 실패(%s) — 원문 채널 접근 권한을 확인해주세요." % sr.get("error"),
                             "username": ASSISTANT_NAME, "icon_emoji": ":warning:"}, post=True)
            changed = True
    if changed:
        _save(d)


if __name__ == "__main__":
    # 수동 점검: 인자로 'test' → 비서 채널 자기 자신을 타깃으로 제안 1건 생성
    import sys
    env = w.load_env(w.ENV_FILE)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        ts = propose(env.get("SLACK_BOT_TOKEN", "").strip(), w.CHANNEL,
                     "[발송 게이트 점검용 메시지 — 승인되면 이 채널로 들어옵니다]", "게이트 점검(자기 채널)")
        print("proposed ts=", ts)
    else:
        process(env)
        print("processed")
