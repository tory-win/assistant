#!/usr/bin/env python3
"""
tory_slack_file.py — Slack 파일(PPT·PDF·엑셀) 본문 텍스트 읽기 (2026-06-15).

토리 조사 엔진이 '채널의 PPT/자료 본문까지' 읽게 한다. files:read 스코프가 user 토큰에
추가돼 있어야 동작한다(없으면 안내 메시지 반환 — 호출부는 graceful).

흐름: files.info(file_id) → converted_pdf(또는 PDF는 url_private_download) 다운로드(토큰 헤더)
      → pdftotext(poppler) 로 텍스트 추출 → 텍스트 반환(상한).

사용: tory_slack_file.py <file_id>
토큰은 ~/.hermes/.env 에서 내부 주입만, 절대 출력하지 않는다. stdlib + pdftotext 만.
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

ENV = PROFILE.get("env_file") or os.path.join(HOME, ".hermes", ".env")
MAX_TEXT = 14000


def _token():
    try:
        for l in open(ENV):
            if l.startswith("SLACK_USER_TOKEN="):
                return l.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _api(method, token, params):
    req = urllib.request.Request(
        "https://slack.com/api/" + method + "?" + urllib.parse.urlencode(params),
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _download(url, token, dest):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())


def read_file(file_id):
    token = _token()
    if not token:
        return "토큰 없음"
    try:
        info = _api("files.info", token, {"file": file_id})
    except Exception as e:
        return "files.info 실패: %s" % str(e)[:120]
    if not info.get("ok"):
        if info.get("error") == "missing_scope":
            return "파일 본문 열람 불가: Slack 앱에 files:read 스코프가 없습니다(앱 재설치 필요)."
        return "files.info 오류: %s" % info.get("error")
    f = info.get("file") or {}
    name = f.get("name") or file_id
    ftype = f.get("filetype")
    url = f.get("converted_pdf") or (f.get("url_private_download") if ftype == "pdf" else None)
    if not url:
        return "본문 추출 불가(변환 PDF 없음): %s [%s]. 링크로 직접 확인: %s" % (name, ftype, f.get("permalink") or "")
    with tempfile.TemporaryDirectory() as td:
        pdf = os.path.join(td, "f.pdf")
        try:
            _download(url, token, pdf)
        except Exception as e:
            return "다운로드 실패(%s): %s" % (name, str(e)[:100])
        head = open(pdf, "rb").read(5)
        if head != b"%PDF-":
            return "다운로드 결과가 PDF가 아닙니다(권한/만료 의심): %s" % name
        try:
            r = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True, timeout=60)
            txt = (r.stdout or "").strip()
        except FileNotFoundError:
            return "pdftotext 미설치(컨테이너 재빌드 필요): %s" % name
        except Exception as e:
            return "텍스트 추출 실패(%s): %s" % (name, str(e)[:100])
    if not txt:
        return "본문이 비어있거나 이미지 위주입니다(텍스트 없음): %s" % name
    return ("[%s 본문 발췌]\n%s" % (name, txt))[:MAX_TEXT]


if __name__ == "__main__":
    print(read_file(sys.argv[1]) if len(sys.argv) > 1 else "usage: tory_slack_file.py <file_id>")
