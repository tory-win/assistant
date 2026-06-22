#!/usr/bin/env python3
"""
tory_research_replier.py — 게이트형 + 대응추적 자동 비서 (2026-06-12).

PROPOSE  : 답할 항목을 '조사할까요?'로 묻고 ✅/❌ 리액션 부착 (조사 X)
FULFILL  : 오승현 ✅(또는 텍스트 응) → 토리(run_claude+4소스)가 조사 → 보고 본문 + 회신 초안(별도 댓글)
TRACK    : 보고 후에도 원 문의가 미답(slack-attention 에 잔존)이면 6시간마다 '아직 미답' 리마인드(원문 링크)
           → 오승현이 원 문의에 답하면 attention 에서 빠지므로 자동 종료(closed).

조사하고 끝이 아니라 '답할 때까지' 추적. 발송은 비서 채널(오승현 본인)로만. 스레드=세션.
"""
import sys
import os
import json
import time
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
SHIM_BIN = os.path.expanduser("~/.torymemory/bin")
if SHIM_BIN != SCRIPT_DIR and SHIM_BIN not in sys.path:
    sys.path.append(SHIM_BIN)
import tory_command_watcher as w
try:
    from tory_format import render_slack, READ_CORE, clean_excerpt
except Exception:
    READ_CORE = ""
    def render_slack(t):
        return re.sub(r"\*\*(.+?)\*\*", r"*\1*", t or "")
    def clean_excerpt(t):
        return (t or "").replace("\n", " ").strip()

# Profile-specific state. Extra assistants run with TORY_ASSISTANT_ID=<id>;
# never let one assistant read another person's research/attention queue.
STATE = getattr(w, "STATE_DIR", os.path.expanduser("~/.torymemory/state"))
ATTN = getattr(w, "SLACK_ATTN", os.path.join(STATE, "slack-attention.json"))
GATE = os.path.join(STATE, "research-gate.json")
WATCHER_STATE = w.STATE_FILE
MAX_PROPOSE = int(os.environ.get("TORY_GATE_PROPOSE", "3"))
TTL = 7 * 86400            # 추적 위해 길게
REMIND_GAP = 6 * 3600      # 미답 리마인드 간격
YES = re.compile(r"(응|네|ㅇㅇ|조사|해줘|해주세요|ㄱㄱ|go|yes|ok|부탁|진행)", re.I)
NO = re.compile(r"(없음|아니|스킵|skip|패스|불필요|괜찮|놔둬|넘어)", re.I)
OK_EMO = "white_check_mark"
NO_EMO = "x"
DONE_EMOS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "ok_hand", "x", "no_entry"}
DONE_TEXT = re.compile(r"(답장|회신).*(불필요|안\s*해도|안해도|필요\s*없|없어도)|"
                       r"(종료|닫아|닫기|완료|처리\s*완료|스킵|skip|패스|불필요)", re.I)
DRAFT_RE = re.compile(r"\[\[DRAFT\]\](.*?)\[\[/DRAFT\]\]", re.S)

REQ_TMPL = (
    "[%s 승인 — 조사·보고] 아래 미답 건의 진짜 의도를 파악하고 Slack·Gmail·Notion·Drive·회의녹음 + "
    "회사 메모리를 교차 조사해 보고하라.\n"
    "[보고 형식] 첫 줄 = 한 줄 결론. 이어서 핵심을 *소제목* + 짧은 불릿으로 묶고, 근거(출처)는 항목 끝에 짧게 명시.\n"
    + READ_CORE +
    "[초안 분리] 상대에게 바로 보낼 회신 초안은 본문 끝에 반드시 `[[DRAFT]] (존댓말 회신문) [[/DRAFT]]` 로 감싸라 — 따로 복사용 댓글로 뽑는다.\n"
    "[채널] %s  [보낸이] %s\n[메시지]\n%s\n[%s 추가 지시] %s"
) % (w.BOSS_NAME, "%s", "%s", "%s", w.BOSS_NAME, "%s")


def _env():
    e = {}
    try:
        for l in open(w.ENV_FILE, encoding="utf-8"):
            l = l.strip()
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                e[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return e


def _register_session_thread(thread_ts):
    try:
        st = json.load(open(WATCHER_STATE))
    except Exception:
        st = {}
    st.setdefault("active_threads", {})[thread_ts] = thread_ts
    try:
        json.dump(st, open(WATCHER_STATE, "w"))
    except OSError:
        pass


def _boss_reacted_any(rxns, names):
    for rx in rxns or []:
        if (rx.get("name") or "").split("::")[0] in names and w.BOSS in (rx.get("users") or []):
            return True
    return False


def _boss_reacted(rxns, name):
    return _boss_reacted_any(rxns, {name})


def _boss_closed_tracking(utok, g):
    """보고/리마인더 스레드에서 보스가 완료 이모지를 달면 '답장 불필요'로 닫는다.

    제안 부모 메시지의 ✅는 조사 승인 의미라 닫기 신호로 보지 않는다. 대신 부모에 ❌를
    나중에 달거나, 부모 아래 보고/초안/리마인더 메시지에 ✅/❌를 달면 닫는다.
    """
    parent_ts = g.get("ts")
    if not (utok and parent_ts):
        return False
    hist = w.slack_call("conversations.history", utok,
                        {"channel": w.CHANNEL, "latest": parent_ts, "inclusive": "true", "limit": 1})
    parent = (hist.get("messages") or [])
    if parent and parent[0].get("ts") == parent_ts and _boss_reacted(parent[0].get("reactions") or [], NO_EMO):
        return True

    rep = w.slack_call("conversations.replies", utok, {"channel": w.CHANNEL, "ts": parent_ts, "limit": 80})
    if not rep.get("ok"):
        return False
    for m in rep.get("messages") or []:
        try:
            if float(m.get("ts", 0)) <= float(parent_ts):
                continue
        except Exception:
            continue
        if _boss_reacted_any(m.get("reactions") or [], DONE_EMOS):
            return True
        if m.get("user") == w.BOSS and DONE_TEXT.search((m.get("text") or "").strip()):
            return True
    return False


def _md(t):
    return render_slack(t)   # 공용 렌더(브리핑·watcher 와 통일)


def main():
    os.makedirs(STATE, exist_ok=True)
    env = _env()
    pk = env.get("OPENAI_API_KEY", "")
    bot = env.get("SLACK_BOT_TOKEN", "")
    utok = env.get("SLACK_USER_TOKEN", "")
    if not (pk and bot and utok):
        print(json.dumps({"skip": "no key/token"})); return
    try:
        items = list((json.load(open(ATTN)).get("open") or {}).values())
    except Exception:
        items = []
    open_keys = {"%s:%s" % (a.get("channel_id"), a.get("ts")) for a in items}
    cand = [a for a in items if a.get("kind") == "mention" and (a.get("excerpt") or "").strip()]
    try:
        gate = json.load(open(GATE))
    except Exception:
        gate = {}
    gate = {k: v for k, v in gate.items() if time.time() - v.get("_t", 0) < TTL}
    researched = proposed = reminded = 0

    for key, g in list(gate.items()):
        ph = g.get("phase")

        # FULFILL — 승인(✅/텍스트) 시 조사·보고
        if ph == "proposed":
            # reactions.get 은 reactions:read 스코프가 없어 missing_scope — history 로 리액션을 읽는다.
            _rh = w.slack_call("conversations.history", utok,
                               {"channel": w.CHANNEL, "latest": g["ts"], "inclusive": "true", "limit": 1})
            _rm = _rh.get("messages") or []
            rxns = (_rm[0].get("reactions") or []) if (_rm and _rm[0].get("ts") == g["ts"]) else []
            approve = _boss_reacted(rxns, OK_EMO)
            reject = _boss_reacted(rxns, NO_EMO)
            extra = ""
            if not (approve or reject):
                rep = w.slack_call("conversations.replies", utok, {"channel": w.CHANNEL, "ts": g["ts"], "limit": 20})
                ur = [m for m in (rep.get("messages") or [])
                      if m.get("user") == w.BOSS and float(m.get("ts", 0)) > float(g["ts"])]
                if ur:
                    txt = (ur[-1].get("text") or "").strip()
                    if NO.search(txt) and not YES.search(txt):
                        reject = True
                    elif YES.search(txt) or txt.replace(" ", "").isdigit():
                        approve = True; extra = txt
            if reject and not approve:
                g["phase"] = "skipped"; g["_t"] = time.time(); continue
            if not approve:
                continue
            req = REQ_TMPL % (g["channel"], g["user"], g["excerpt"], extra)
            # render_prompt: PROMPT_TMPL 의 todo_context 등 누락 필드를 기본값으로 채워 KeyError 회피
            # (이전엔 % {3키}라 FULFILL 이 KeyError 로 죽던 회귀 — 2026-06-15 수정)
            prompt = w.render_prompt(when=time.strftime("%m/%d %H:%M"), request=req,
                                     context="원 문의 perma: %s" % g.get("perma", ""))
            ok, out, err = w.run_claude(prompt, pk)
            if not (ok and out.strip()) or out.strip() == "NO_REPLY":
                g["phase"] = "failed"; g["_t"] = time.time(); continue
            cleaned, _ = w.extract_handoffs(out, {"text": req})
            m = DRAFT_RE.search(cleaned)
            draft = m.group(1).strip() if m else ""
            body = _md(DRAFT_RE.sub("", cleaned).strip())
            params = {"channel": w.CHANNEL, "thread_ts": g["ts"], "text": body[:w.MAX_REPLY],
                      "username": w.ASSISTANT_NAME, "icon_emoji": ":mag:",
                      "unfurl_links": "false", "unfurl_media": "false"}
            try:                                  # 가독성: Block Kit, 실패 시 text 폴백
                blocks, fb = w.to_blocks(body, footer="%s · Slack·Gmail·Notion·Drive·회의녹음 조사" % w.ASSISTANT_NAME)
                if blocks:
                    params["blocks"] = json.dumps(blocks, ensure_ascii=False)
                    params["text"] = (fb or body)[:w.MAX_REPLY]
            except Exception:
                pass
            rr = w.slack_call("chat.postMessage", bot, params, post=True)
            if not rr.get("ok") and "blocks" in params:
                params.pop("blocks", None)
                rr = w.slack_call("chat.postMessage", bot, params, post=True)
            if draft:
                w.slack_call("chat.postMessage", bot,
                             {"channel": w.CHANNEL, "thread_ts": g["ts"],
                              "text": "✍️ *회신 초안* — 탭해서 복사하세요\n```%s```" % _md(draft)[:1800],
                              "username": w.ASSISTANT_NAME, "icon_emoji": ":pencil2:",
                              "unfurl_links": "false", "unfurl_media": "false"}, post=True)
            _register_session_thread(g["ts"])
            if rr.get("ts"):
                g["report_ts"] = rr.get("ts")
            g["phase"] = "reported"; g["last_remind"] = time.time(); g["_t"] = time.time()
            researched += 1

        # TRACK — 보고 후에도 원 문의 미답이면 리마인드, 답하면 종료
        elif ph == "reported":
            if _boss_closed_tracking(utok, g):
                g["phase"] = "closed"; g["closed_reason"] = "boss_reaction"; g["_t"] = time.time(); continue
            if key not in open_keys:        # attention 에서 빠짐 = 오승현이 답함
                g["phase"] = "closed"; g["_t"] = time.time(); continue
            if time.time() - g.get("last_remind", 0) >= REMIND_GAP:
                msg = ("⏰ *아직 답 안 하셨어요* — `#%s · %s` 건입니다.\n"
                       "조사 보고는 이 스레드 위에 있어요. 원문: %s\n"
                       "답장 안 해도 되는 건 이 리마인더나 위 보고에 ✅ 리액션을 달면 닫을게요."
                       % (g["channel"], g["user"], g.get("perma", "")))
                rr = w.slack_call("chat.postMessage", bot,
                                  {"channel": w.CHANNEL, "thread_ts": g["ts"], "text": msg, "username": w.ASSISTANT_NAME,
                                   "icon_emoji": ":alarm_clock:", "unfurl_links": "false"}, post=True)
                if rr.get("ts"):
                    g["last_remind_ts"] = rr.get("ts")
                g["last_remind"] = time.time(); reminded += 1

    # PROPOSE — 새 답할 항목 제안 + ✅/❌ 리액션
    for a in cand:
        key = "%s:%s" % (a.get("channel_id"), a.get("ts"))
        if key in gate:
            continue
        if proposed >= MAX_PROPOSE:
            break
        msg = ("📋 *조사 제안* — `#%s · %s`\n> %s\n"
               "✅ = 조사·보고   ❌ = 스킵   _(이모지로. 지시 추가는 답글)_"
               % (a.get("channel"), a.get("user"), clean_excerpt(a.get("excerpt") or "")[:140]))
        r = w.slack_call("chat.postMessage", bot,
                         {"channel": w.CHANNEL, "text": msg, "username": w.ASSISTANT_NAME,
                          "icon_emoji": ":clipboard:", "unfurl_links": "false", "unfurl_media": "false"}, post=True)
        if r.get("ok"):
            ts = r.get("ts")
            w.slack_call("reactions.add", bot, {"channel": w.CHANNEL, "timestamp": ts, "name": OK_EMO})
            w.slack_call("reactions.add", bot, {"channel": w.CHANNEL, "timestamp": ts, "name": NO_EMO})
            gate[key] = {"phase": "proposed", "ts": ts, "channel": a.get("channel"),
                         "user": a.get("user"), "excerpt": a.get("excerpt"),
                         "perma": a.get("permalink"), "_t": time.time()}
            proposed += 1
    try:
        json.dump(gate, open(GATE, "w"))
    except OSError:
        pass
    print(json.dumps({"proposed": proposed, "researched": researched, "reminded": reminded,
                      "gate_open": len(gate)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
