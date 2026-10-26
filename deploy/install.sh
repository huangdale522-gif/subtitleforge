#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  if command -v openssl >/dev/null 2>&1; then
    token=$(openssl rand -hex 24)
  else
    token=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
  fi
  sed -i.bak "s/^SF_API_TOKEN=.*/SF_API_TOKEN=${token}/" .env
  rm -f .env.bak
  echo "Created .env with a generated API token."
fi

mkdir -p data
docker compose pull || true
docker compose up -d --build
echo "SubtitleForge is starting on port 8600."
echo "Check: curl http://127.0.0.1:8600/healthz"

