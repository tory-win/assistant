# Migration

`/Users/tory/Downloads/dev/assistant`를 비서 코드 정본으로 쓰기 위한 전환 기록과 운영 절차다.

## 현재 상태

- 소스 정본: `/Users/tory/Downloads/dev/assistant`
- 라이브 스크립트 마운트: `/Users/tory/Downloads/dev/assistant/bin -> /root/.torymemory/bin`
- 라이브 컨테이너: `assistant-agent`, `assistant-agent-yujeong`

## 코드 반영

`bin/` 수정은 bind mount로 다음 루프부터 반영된다. `docker/`나 compose 변경 후에는 컨테이너를 재생성한다.

## 컨테이너 전환

초기 전환은 2026-06-22에 완료했다. 다시 수행할 때도 기존 컨테이너와 동시에 켜면 Slack/Gmail/Notion 큐를 중복 처리할 수 있다.

```sh
cd /Users/tory/Downloads/dev/assistant
docker compose up -d --build
docker compose -f docker-compose.yujeong.yml up -d --build
```

## 검증

```sh
docker ps --format '{{.Names}}' | grep 'assistant-agent'
docker compose -f /Users/tory/Downloads/dev/assistant/docker-compose.yml logs --tail=80
docker compose -f /Users/tory/Downloads/dev/assistant/docker-compose.yujeong.yml logs --tail=80
```
