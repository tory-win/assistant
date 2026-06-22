#!/usr/bin/env python3
"""tory_research_oneshot.py — 특정 문의 하나를 토리 조사엔진(run_claude+4소스)으로 검토해
비서 채널에 토리 명의로 보고 게시. 3단계(brief 자동 트리거)의 수동 실행판/시연(2026-06-12).
인자: argv[1]=request(검토 지시), argv[2]=context(원문의 메타)."""
import sys
import os
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
SHIM_BIN = os.path.expanduser("~/.torymemory/bin")
if SHIM_BIN != SCRIPT_DIR and SHIM_BIN not in sys.path:
    sys.path.append(SHIM_BIN)
import tory_command_watcher as w  # run_claude, slack_call, extract_handoffs, PROMPT_TMPL, CHANNEL 재사용


def _env():
    e = {}
    for l in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
        l = l.strip()
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1)
            e[k.strip()] = v.strip().strip('"').strip("'")
    return e


def main():
    request = sys.argv[1] if len(sys.argv) > 1 else ""
    context = sys.argv[2] if len(sys.argv) > 2 else ""
    env = _env()
    proxy_key = env.get("OPENAI_API_KEY", "")
    bot = env.get("SLACK_BOT_TOKEN", "")
    if not (proxy_key and bot):
        print(json.dumps({"ok": False, "err": "no key/bot"})); return
    prompt = w.PROMPT_TMPL % {"when": time.strftime("%m/%d %H:%M"), "request": request, "context": context}
    ok, out, err = w.run_claude(prompt, proxy_key)
    if not (ok and out.strip()) or out.strip() == "NO_REPLY":
        print(json.dumps({"ok": False, "err": (err or out or "")[:400]}, ensure_ascii=False)); return
    cleaned, _ = w.extract_handoffs(out, {"text": request})
    header = ("🔎 *토리 자동 검토 — 고한솔님 푸시알림 과금 문의 (재검토)*\n"
              "원문의 의도를 다시 파악해 Slack·Gmail·Notion·Drive 4소스로 조사했습니다. 방향은 본부장님이 결정해 주세요.\n\n")
    r = w.slack_call("chat.postMessage", bot,
                     {"channel": w.CHANNEL, "text": header + cleaned, "username": "토리",
                      "icon_emoji": ":card_index_dividers:", "unfurl_links": "false", "unfurl_media": "false"},
                     post=True)
    print(json.dumps({"posted": r.get("ok"), "err": r.get("error"), "len": len(cleaned)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
