#!/bin/sh
# 토리 비서 킬스위치 — 즉시 정지(폴러·브리핑·큐레이터). 토큰 폐기는 안내만(수동, 되돌릴 수 없으므로).
set -u
UID_="$(id -u)"
echo "[killswitch] 토리 정지 시작..."
for a in com.tory.hermes-slack-fetch com.tory.hermes-slack-brief com.tory.hermes-google-fetch com.tory.tory-command-watcher; do
  if launchctl bootout "gui/$UID_/$a" 2>/dev/null; then
    echo "  - launchd 정지: $a"
  else
    echo "  - (이미 정지/없음): $a"
  fi
done
if docker stop torymemory-hermes-curator >/dev/null 2>&1; then
  echo "  - 큐레이터 컨테이너 정지"
else
  echo "  - 큐레이터 정지 실패/없음"
fi
echo
echo "[killswitch] 토큰까지 무효화하려면(수동):"
echo "  Slack : api.slack.com/apps → 토리 → OAuth & Permissions → Revoke Tokens"
echo "  Google: myaccount.google.com/permissions → 토리 제거"
echo
echo "[killswitch] 재가동:"
echo "  launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.tory.hermes-slack-fetch.plist"
echo "  launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.tory.hermes-slack-brief.plist"
echo "  launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.tory.hermes-google-fetch.plist"
echo "  launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.tory.tory-command-watcher.plist"
echo "  docker start torymemory-hermes-curator"
echo "[killswitch] 완료."
