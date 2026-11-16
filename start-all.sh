#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
CMD="${1:-up}"; PROFILE="${2:-core}"; shift || true; shift || true || true
[ -f .env ] || { echo "[info] .env fehlt -> von .env.example kopieren und Secrets setzen"; }
case "$CMD" in
  up)      docker compose --profile "$PROFILE" up -d "$@" ;;
  down)    docker compose --profile "$PROFILE" down "$@" ;;
  restart) docker compose --profile "$PROFILE" restart "$@" ;;
  logs)    docker compose --profile "$PROFILE" logs -f --tail=200 "$@" ;;
  ps|status) docker compose --profile "$PROFILE" ps "$@" ;;
  pull)    docker compose --profile "$PROFILE" pull "$@" ;;
  build)   docker compose --profile "$PROFILE" build "$@" ;;
  config)  docker compose --profile "$PROFILE" config "$@" ;;
  *) echo "usage: $0 <up|down|restart|logs|ps|pull|build|config> [profile: core|ai|edge|commerce|ops|growth|full]"; exit 2;;
esac
