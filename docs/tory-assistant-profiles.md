# Tory Assistant Profiles

토리 비서는 공용 엔진 하나를 사람별 프로필로 분리해 실행한다.
코드를 사람마다 복제하지 않는다.

## 구조

- 공용 코드: `/Users/tory/Downloads/dev/assistant/bin`
- 사람별 프로필: `/Users/tory/.torymemory/assistants/<assistant_id>.json`
- 사람별 Slack 설정: `/Users/tory/.torymemory/assistants/<assistant_id>/slack-config.json`
- 사람별 상태/승인함: `/Users/tory/.torymemory/assistants/<assistant_id>/state`, `outbox`, `feeds`
- 실행 분리: `TORY_ASSISTANT_ID=<assistant_id>` 컨테이너

사람별로 분리해야 하는 값은 boss name, Slack user id, assistant channel id,
Notion task DB id, Notion task owner id/name, enabled sources/actions 이다.
Slack fetch 는 등록된 모든 비서 출력 채널을 자동 제외해야 한다. 비서 채널은
명령/브리핑용 inbox 이지 다른 비서의 수집 소스가 아니다.

## 추가 절차

1. `/Users/tory/.torymemory/assistants/_template.json`을 복사해
   `/Users/tory/.torymemory/assistants/<assistant_id>.json`을 만든다.
2. Slack에서 사용자 id와 비서 채널 id를 넣는다.
3. Notion DB URL에서 `?v=` 앞의 database id를 넣는다. `?v=` 값은 view id라 DB id가 아니다.
4. Notion `담당자` 속성이 `people`이면 Notion user id를 `notion_task_owner_id`에 넣는다.
   `relation`이면 이름(`notion_task_owner_name`)으로 relation DB에서 자동 해석할 수 있다.
5. `enabled_sources`와 `enabled_actions`를 정한다.
   예: Slack+Notion only는 `["slack", "notion"]`.
6. `docker-compose.<assistant_id>.yml`을 만들고
   `TORY_ASSISTANT_ID=<assistant_id>`와 고유 container name을 지정한다.
7. 실행한다.

```bash
docker compose -f docker-compose.<assistant_id>.yml up -d --build
```

## 검증

```bash
docker exec assistant-agent-<assistant_id> sh -lc 'echo $TORY_ASSISTANT_ID'
docker exec assistant-agent-<assistant_id> sh -lc 'python3 /root/.torymemory/bin/tory_notion_tasks.py'
docker exec assistant-agent-<assistant_id> sh -lc 'python3 /root/.torymemory/bin/torymemory_slack_fetch.py --discover'
docker logs assistant-agent-<assistant_id> --tail 120
```

정상 상태:

- 컨테이너가 `Up`.
- 프로필의 `state_dir/outbox_dir`가 `/root/.torymemory/assistants/<assistant_id>/...`.
- 도구 목록이 enabled sources/actions 범위만 포함.
- Notion task 조회가 해당 담당자 기준으로 나온다.
- 비활성 소스는 `*_sources_disabled`로 skip된다.

## 김유정 프로필

- assistant id: `yujeong`
- Slack user id: `U03EVPNUREG`
- assistant channel id: `C0BAEFGNH55`
- Notion task DB: `1a3ea3ff-6c9b-80df-ad29-c2c7863caca0` (`프로덕트 하위 업무`)
- Notion owner id: `9e86de84-d41a-415f-be8d-18fd43177952`
- enabled sources/actions: Slack, Notion
- compose file: `docker-compose.yujeong.yml`
