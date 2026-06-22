# Assistant

토리 비서 코드베이스를 `torymemory` 인프라에서 분리한 저장소다.

현재 라이브 운영은 아직 `~/.torymemory/bin` 마운트를 쓰고 있고, 이 저장소는 그 최신본을 가져온 독립 소스다. 운영 전환은 기존 `torymemory-tory-agent*` 컨테이너를 내린 뒤 별도 단계로 한다.

## 구조

- `bin/`: 비서 런타임 스크립트. Slack/Gmail/Calendar/Drive/Notion fetch, brief, command watcher, send gate, research replier.
- `docker/tory-agent/`: 6-job agent 컨테이너 이미지와 entrypoint.
- `docker-compose.yml`: 기본 토리 비서 compose. 기존 운영 컨테이너와 충돌하지 않게 `assistant-agent` 이름을 쓴다.
- `docker-compose.yujeong.yml`: 유정 비서 compose.
- `config/assistants/`: 프로필 템플릿과 현재 프로필 예시. secret 은 없다.
- `docs/토리_비서.md`: 현재 운영 규칙 요약.
- `scripts/`: smoke test, 라이브 동기화 보조 스크립트.

## 검증

```sh
cd /Users/tory/Downloads/dev/assistant
scripts/smoke-test.sh
```

## 개발 루프

1. 이 저장소에서 수정한다.
2. `scripts/smoke-test.sh`로 문법과 compose shape를 확인한다.
3. 필요하면 `CONFIRM=install scripts/install-to-live.sh`로 `~/.torymemory/bin`에 반영한다.
4. 라이브 컨테이너 로그와 상태 파일로 실제 동작을 확인한다.

## 주의

- `docker compose up`은 현재 라이브 비서와 동시에 돌리면 같은 Slack/Gmail/Notion 큐를 중복 처리할 수 있다.
- 운영 전환 전에는 smoke test와 정적 검증만 한다.
- `.env`, Slack/Google/Notion token, state/feed/outbox 파일은 절대 커밋하지 않는다.

