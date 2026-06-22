# Migration

`/Users/tory/Downloads/dev/assistant`를 비서 코드 정본으로 쓰기 위한 전환 절차다.

## 현재 상태

- 소스 정본: `/Users/tory/Downloads/dev/assistant`
- 라이브 스크립트 마운트: `~/.torymemory/bin`
- 라이브 컨테이너: `torymemory-tory-agent`, `torymemory-tory-agent-yujeong`

## 코드 반영

```sh
cd /Users/tory/Downloads/dev/assistant
scripts/smoke-test.sh
CONFIRM=install scripts/install-to-live.sh
```

## 컨테이너 전환

운영 컨테이너 이름까지 assistant repo 기준으로 옮길 때만 수행한다. 기존 컨테이너와 동시에 켜면 Slack/Gmail/Notion 큐를 중복 처리할 수 있다.

```sh
docker compose -f /Users/tory/Downloads/dev/torymemory/docker-compose.tory-agent.yml down
docker compose -f /Users/tory/Downloads/dev/torymemory/docker-compose.tory-agent.yujeong.yml down
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

