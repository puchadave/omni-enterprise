#!/usr/bin/env python3
import re, sys
from pathlib import Path

ROOT = Path('/mnt/social.puchalla.pro/omni-enterprise')

ZT_ENV_BLOCK = '''
# --- ZeroTier / Multi-Device Networking (optional, for standalone on different hosts) ---
# Prefer host-installed ZeroTier (scripts/zerotier-host-setup.sh). Container sidecar via profile `zt`.
ZT_ENABLED=false
ZT_NETWORK_ID=
ZT_NODE_NAME=
ZEROTIER_IMAGE=zerotier/zerotier:latest
# Bind published ports (security): 0.0.0.0 (default), 127.0.0.1 (local-only), or a specific ZT IP
SERVICE_BIND_HOST=0.0.0.0
'''

ZT_SERVICE = r'''
  zerotier:
    image: ${ZEROTIER_IMAGE:-zerotier/zerotier:latest}
    container_name: ${COMPOSE_PROJECT_NAME:-omni}-zerotier
    restart: unless-stopped
    profiles: ["zt"]
    network_mode: host
    cap_add:
      - NET_ADMIN
      - SYS_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    volumes:
      - zerotier_data:/var/lib/zerotier-one
    environment:
      ZT_NETWORK_ID: ${ZT_NETWORK_ID:-}
    command: ["sh","-lc","zerotier-one -d; sleep 3; if [ -n \"${ZT_NETWORK_ID:-}\" ]; then zerotier-cli join ${ZT_NETWORK_ID} || true; fi; tail -f /dev/null"]
    healthcheck:
      test: ["CMD-SHELL", "zerotier-cli info >/dev/null 2>&1 || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 20s
'''

def add_env(path: Path, extra=''):
    s = path.read_text()
    if 'ZT_NETWORK_ID' in s:
        return False
    if not s.endswith('\n'): s += '\n'
    s += ZT_ENV_BLOCK
    if extra:
        s += extra
    path.write_text(s)
    return True

def add_zerotier_service_and_volume(compose: Path):
    s = compose.read_text()
    changed = False
    if '\n  zerotier:\n' not in s:
        # insert before 'volumes:' (top-level) if exists else before 'networks:' else append
        m = re.search(r'\nvolumes:\n', s)
        if m:
            s = s[:m.start()] + '\n' + ZT_SERVICE + '\n' + s[m.start()+1:]
        else:
            m2 = re.search(r'\nnetworks:\n', s)
            if m2:
                s = s[:m2.start()] + '\n' + ZT_SERVICE + '\n' + s[m2.start()+1:]
            else:
                s += '\n' + ZT_SERVICE + '\n'
        changed = True
    # ensure volume zerotier_data exists
    if re.search(r'^volumes:\s*$', s, re.M):
        if not re.search(r'^\s{2}zerotier_data:', s, re.M):
            # add after 'volumes:' line
            s = re.sub(r'^volumes:\s*$', 'volumes:\n  zerotier_data:\n    name: ${COMPOSE_PROJECT_NAME:-omni}_zerotier_data', s, count=1, flags=re.M)
            changed = True
    else:
        # append volumes block
        s += '\nvolumes:\n  zerotier_data:\n    name: ${COMPOSE_PROJECT_NAME:-omni}_zerotier_data\n'
        changed = True
    if changed:
        compose.write_text(s)
    return changed

def bind_ports(compose: Path):
    s = compose.read_text()
    orig = s
    # replace simple "- \"8002:8000\"" or "- \"${PORT:-x}:y\"" style not already bound
    def repl(m):
        pre, portmap, post = m.group(1), m.group(2), m.group(3)
        if portmap.startswith('${SERVICE_BIND_HOST') or portmap.startswith('127.0.0.1') or portmap.startswith('0.0.0.0:'):
            return m.group(0)
        # avoid replacing non-port strings? portmap contains :
        if ':' not in portmap:
            return m.group(0)
        return f'{pre}"${{SERVICE_BIND_HOST:-0.0.0.0}}:{portmap}"{post}'
    s = re.sub(r'(\n\s+-\s+)"([^"]*:\d+(?:/\w+)?)"(\s*(?:#.*)?)', repl, s)
    # also unquoted - 8002:8000
    def repl2(m):
        pre, portmap, post = m.group(1), m.group(2), m.group(3)
        if '${SERVICE_BIND_HOST' in portmap or portmap.startswith('127.0.0.1') or portmap.startswith('0.0.0.0:'):
            return m.group(0)
        if ':' not in portmap: return m.group(0)
        return f'{pre}"${{SERVICE_BIND_HOST:-0.0.0.0}}:{portmap}"{post}'
    s = re.sub(r'(\n\s+-\s+)([^\s"\n][^\n]*:\d+(?:/\w+)?)(\s*(?:#.*)?$)', repl2, s, flags=re.M)
    if s != orig:
        compose.write_text(s)
        return True
    return False

modules = sorted([p for p in (ROOT/'modules').iterdir() if p.is_dir()])
for m in modules:
    env = m/'.env.example'
    comp = m/'docker-compose.yml'
    if env.exists():
        add_env(env)
    if comp.exists():
        add_zerotier_service_and_volume(comp)
        bind_ports(comp)

# root env
root_env = ROOT/'.env.example'
if root_env.exists():
    add_env(root_env, extra='''# --- Resilience / URLs (override for multi-device/ZeroTier peers) ---
MIDDLEWARE_INTERNAL_URL=http://middleware:8000
OMNIROUTE_INTERNAL_URL=http://omniroute:20128/v1
''')

# swarm env
swarm_env = ROOT/'stacks/swarm/.env.example'
if swarm_env.exists():
    add_env(swarm_env)

print('patched modules/root/swarm env+compose')
