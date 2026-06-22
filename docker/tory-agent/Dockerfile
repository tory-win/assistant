# 토리 자동화 에이전트 — fetch×3·brief·watcher·replier 6잡을 한 컨테이너로.
# 조사 엔진은 tory_agent_llm 의 HTTP 도구루프(host :8321 messages API) — claude -p 바이너리 불필요.
#   → claude 버전/모델 502 리스크 완전 제거.
# 런타임: python3 + /usr/bin/curl(slack/notion .sh) + bash.
#   google_fetch 만 서드파티 google 라이브러리 필요(캘린더·Gmail 브리핑 데이터). gmail_read/drive_read 헬퍼는 raw urllib.
# 재기동 견고: Docker Desktop 기동 후 compose 의 restart:unless-stopped 로 자동 복귀.
# macOS 로그인 전 무인 기동은 Docker Desktop/autologin 설정에 의존한다.
FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ca-certificates tzdata curl bash poppler-utils \
    && pip3 install --no-cache-dir --break-system-packages \
         google-auth google-api-python-client requests \
    && rm -rf /var/lib/apt/lists/*
# poppler-utils = pdftotext: 슬랙 파일(PPT/PDF) converted_pdf 본문 텍스트 추출(tory_slack_file.py)
ENV TZ=Asia/Seoul
RUN mkdir -p /app/bin

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
WORKDIR /app
ENTRYPOINT ["/entrypoint.sh"]
