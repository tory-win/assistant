#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "$ROOT"/bin/*.py

for script in "$ROOT"/bin/*.sh "$ROOT"/scripts/*.sh; do
  [ -e "$script" ] || continue
  sh -n "$script"
done

if command -v docker >/dev/null 2>&1; then
  docker compose -f "$ROOT/docker-compose.yml" config >/dev/null
  docker compose -f "$ROOT/docker-compose.yujeong.yml" config >/dev/null
fi

if command -v rg >/dev/null 2>&1; then
  rg -n "(xox[baprs]-[0-9A-Za-z-]{20,}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)" "$ROOT" \
    --glob '!*.bak*' --glob '!README.md' --glob '!scripts/smoke-test.sh' && {
      echo "possible secret matched" >&2
      exit 1
    }
fi

echo "smoke-test ok"
