# Assistant

토리 비서 코드베이스를 `torymemory` 인프라에서 분리한 저장소다.

현재 라이브 운영은 이 저장소의 compose가 담당한다. 컨테이너 `assistant-agent`, `assistant-agent-yujeong`이 이 저장소의 `bin/`을 `/app/bin`으로 마운트한다.

`~/.torymemory/bin`은 일부 macOS launchd/TCC 작업과 과거 호출자를 위한 호환 shim 으로만 남긴다. 새 비서 코드와 운영 문서는 이 저장소가 정본이다.

## 구조

- `bin/`: 비서 런타임 스크립트. Slack/Gmail/Calendar/Drive/Notion fetch, brief, command watcher, send gate, research replier.
- `docker/tory-agent/`: 6-job agent 컨테이너 이미지와 entrypoint.
- `docker-compose.yml`: 기본 토리 비서 compose. 기존 운영 컨테이너와 충돌하지 않게 `assistant-agent` 이름을 쓴다.
- `docker-compose.yujeong.yml`: 유정 비서 compose.
- `config/assistants/`: 프로필 템플릿과 현재 프로필 예시. secret 은 없다.
- `docs/토리_비서.md`: 현재 운영 규칙 요약.
- `scripts/`: smoke test, 호환 shim 동기화 보조 스크립트.

## 검증

```sh
cd /Users/tory/Downloads/dev/assistant
scripts/smoke-test.sh
```

## 개발 루프

1. 이 저장소에서 수정한다.
2. `scripts/smoke-test.sh`로 문법과 compose shape를 확인한다.
3. `bin/` 수정은 bind mount로 다음 루프부터 반영된다. `docker/`나 compose 변경은 컨테이너를 재생성한다.
4. 라이브 컨테이너 로그와 상태 파일로 실제 동작을 확인한다.

## 주의

- 구 비서 에이전트 컨테이너를 되살리면 같은 Slack/Gmail/Notion 큐를 중복 처리할 수 있다.
- `.env`, Slack/Google/Notion token, state/feed/outbox 파일은 절대 커밋하지 않는다.
