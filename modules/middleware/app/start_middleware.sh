#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PORT="${OMNI_CORE_PORT:-8002}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

# Port-Check
if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "Port ${PORT} belegt."
  PID="$(ss -ltnp 2>/dev/null | awk -v p=":${PORT} " '$4 ~ p {print $NF}' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)"
  echo "PID=${PID:-unbekannt}"
  echo "Health: $(curl -sS -m 3 -o /dev/null -w '%{http_code}' http://localhost:${PORT}/api/health 2>/dev/null || echo n/a)"
  exit 0
fi

# venv aktivieren (falls vorhanden)
if [ -f "$ROOT/.venv/bin/activate" ]; then . "$ROOT/.venv/bin/activate"; elif [ -f "$ROOT/venv/bin/activate" ]; then . "$ROOT/venv/bin/activate"; fi

# automatischer DB-Check (Schema/Verbindung über init)
python3 - <<'PY'
import asyncio
from database import init_db
asyncio.run(init_db())
print("db_check=ok")
PY

export OMNI_CORE_PORT="$PORT"
exec uvicorn main:app --host 0.0.0.0 --port "$PORT" >>"$LOG_DIR/middleware_${PORT}.log" 2>&1
