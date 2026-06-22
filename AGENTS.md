# Codex 세션 진입

이 저장소는 토리/멤버별 비서 코드베이스다. 공통 AI 행동 지침은 홈 레벨 SSOT 를 따른다.

## 경계

- 코드 정본: 이 저장소의 `bin/`, `docker/`, `docker-compose*.yml`.
- 라이브 런타임: `~/.torymemory/bin`, `~/.torymemory/state`, `~/.torymemory/feeds`, `~/.torymemory/assistants`.
- secret/token/state/feed 는 이 저장소에 넣지 않는다.
- 토리메모리 큐레이터/Hermes/OpenMemory 서버 코드는 이 저장소 범위 밖이다.

## 작업 규칙

- 라이브 상태는 추정하지 말고 `docker ps`, 실제 마운트, state JSON 으로 확인한다.
- 비서 발송 게이트 불변식은 유지한다: 타인에게 가는 Slack/Gmail/Notion 실행은 보스 승인 후에만.
- 프로필별 state/feed/outbox 격리를 깨지 않는다.
- 변경 후 최소 검증은 `scripts/smoke-test.sh`.

