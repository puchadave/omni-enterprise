#!/usr/bin/env bash
# Common helpers for Shopware Proxmox one-click deployment.
# shellcheck disable=SC2034

set -Eeuo pipefail

SCRIPT_VERSION="1.0.0"
DEFAULT_CONFIG_FILE=""
SCRIPT_DIR=""
DEPLOY_ROOT=""
LIB_DIR=""

_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log() {
  local level="$1"; shift
  local msg="$*"
  local line="$(_ts) [${level}] ${msg}"
  echo "$line" >&2
  if [[ -n "${LOG_FILE:-}" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$line" >> "$LOG_FILE"
  fi
}

log_info()  { log INFO  "$*"; }
log_warn()  { log WARN  "$*"; }
log_error() { log ERROR "$*"; }
log_ok()    { log OK    "$*"; }
log_step()  { log STEP  "$*"; }

die() {
  local code="${2:-1}"
  log_error "$1"
  record_step "${CURRENT_STEP:-unknown}" "failed" "$1" "$code"
  exit "$code"
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: $cmd" 127
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

yaml_get() {
  # yaml_get <file> <dot.path> [default]
  local file="$1" path="$2" default="${3:-}"
  python3 - "$file" "$path" "$default" <<'PY'
import sys, json
try:
    import yaml
except Exception:
    yaml = None

file, path, default = sys.argv[1], sys.argv[2], sys.argv[3]

def load_yaml(p):
    with open(p, 'r', encoding='utf-8') as f:
        text = f.read()
    if yaml:
        return yaml.safe_load(text) or {}
    # Minimal fallback parser for simple key: value and nested maps/lists used in config.yaml
    data = {}
    stack = [(0, data)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        line = raw.strip()
        if line.startswith('- '):
            continue
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        k = k.strip(); v = v.strip()
        while stack and indent < stack[-1][0]:
            stack.pop()
        cur = stack[-1][1]
        if v == '':
            cur[k] = {}
            stack.append((indent + 2, cur[k]))
        else:
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            elif v.startswith("'") and v.endswith("'"):
                v = v[1:-1]
            elif v.lower() in ('true','false'):
                v = v.lower() == 'true'
            elif v.lower() in ('null','~'):
                v = None
            elif v.startswith('[') and v.endswith(']'):
                inner = v[1:-1].strip()
                if inner:
                    parts = [p.strip().strip('"').strip("'") for p in inner.split(',')]
                    v = parts
                else:
                    v = []
            else:
                try:
                    if '.' in v:
                        raise ValueError
                    v = int(v)
                except Exception:
                    pass
            cur[k] = v
    return data

try:
    data = load_yaml(file)
    cur = data
    for part in path.split('.'):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            cur = None
            break
    if cur is None:
        print(default)
    elif isinstance(cur, bool):
        print('true' if cur else 'false')
    elif isinstance(cur, (list, dict)):
        print(json.dumps(cur))
    else:
        print(cur)
except Exception:
    print(default)
PY
}

init_paths() {
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  DEPLOY_ROOT="$SCRIPT_DIR"
  LIB_DIR="$SCRIPT_DIR/lib"
  DEFAULT_CONFIG_FILE="$SCRIPT_DIR/config.yaml"
}

init_runtime() {
  CONFIG_FILE="${CONFIG_FILE:-$DEFAULT_CONFIG_FILE}"
  [[ -f "$CONFIG_FILE" ]] || die "Config file not found: $CONFIG_FILE"

  LOG_DIR="$(yaml_get "$CONFIG_FILE" deployment.log_dir /var/log/shopware-deploy)"
  STATE_DIR="$(yaml_get "$CONFIG_FILE" deployment.state_dir /var/lib/shopware-deploy)"
  LOCK_FILE="$(yaml_get "$CONFIG_FILE" deployment.lock_file /var/lock/shopware-deploy.lock)"
  REPORT_FILE="$(yaml_get "$CONFIG_FILE" deployment.report_file /var/log/shopware-deploy/deployment-report.json)"
  LOG_FILE="$LOG_DIR/deploy.log"
  STEPS_JSON="$STATE_DIR/steps.json"
  mkdir -p "$LOG_DIR" "$STATE_DIR"
  echo '[]' > "$STEPS_JSON"
}

CURRENT_STEP=""
record_step() {
  local step="$1" status="$2" message="${3:-}" code="${4:-0}"
  python3 - "$STEPS_JSON" "$step" "$status" "$message" "$code" <<'PY'
import json,sys,os,datetime
path,step,status,message,code=sys.argv[1:6]
now=datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
try:
    with open(path,'r',encoding='utf-8') as f: data=json.load(f)
except Exception:
    data=[]
data.append({"step":step,"status":status,"message":message,"exit_code":int(code),"timestamp":now})
with open(path,'w',encoding='utf-8') as f: json.dump(data,f,indent=2)
PY
}

run_step() {
  local step="$1"; shift
  CURRENT_STEP="$step"
  log_step "START: $step"
  record_step "$step" "started" "" 0
  if "$@"; then
    log_ok "DONE: $step"
    record_step "$step" "success" "" 0
  else
    local rc=$?
    log_error "FAILED: $step (exit $rc)"
    record_step "$step" "failed" "command failed" "$rc"
    return "$rc"
  fi
}

acquire_lock() {
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    die "Another deployment is running (lock: $LOCK_FILE)" 75
  fi
}

random_password() {
  local len="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9!@#%^_+=' | head -c "$len"
  else
    tr -dc 'A-Za-z0-9!@#%^_+=' </dev/urandom | head -c "$len"
  fi
  echo
}

ensure_secret() {
  local name="$1" value="$2" dir="$3"
  mkdir -p "$dir"
  chmod 700 "$dir"
  local file="$dir/$name"
  if [[ -n "$value" ]]; then
    printf '%s' "$value" > "$file"
  elif [[ ! -s "$file" ]]; then
    random_password 32 > "$file"
  fi
  chmod 600 "$file"
  cat "$file"
}

is_proxmox_host() {
  command -v pct >/dev/null 2>&1 && [[ -d /etc/pve ]]
}

is_root() { [[ "$(id -u)" -eq 0 ]]; }

safe_pct_exec() {
  local ctid="$1"; shift
  pct exec "$ctid" -- bash -lc "$*"
}

http_check() {
  local url="$1" timeout="${2:-10}"
  curl -ksS --max-time "$timeout" -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo "000"
}

write_json_report() {
  local status="$1" message="${2:-}"
  python3 - "$REPORT_FILE" "$STEPS_JSON" "$status" "$message" "$CONFIG_FILE" "$SCRIPT_VERSION" <<'PY'
import json,sys,os,datetime
report,steps,status,message,cfg,ver=sys.argv[1:7]
now=datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
try:
    with open(steps,'r',encoding='utf-8') as f: st=json.load(f)
except Exception:
    st=[]
doc={
  "status": status,
  "message": message,
  "timestamp": now,
  "version": ver,
  "config_file": os.path.abspath(cfg),
  "steps": st,
}
os.makedirs(os.path.dirname(report), exist_ok=True)
with open(report,'w',encoding='utf-8') as f: json.dump(doc,f,indent=2)
print(report)
PY
}
