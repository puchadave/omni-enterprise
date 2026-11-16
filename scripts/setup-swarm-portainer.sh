#!/usr/bin/env bash
# setup-swarm-portainer.sh
# Interaktives, menü-geführtes Setup für Docker Swarm + Portainer (Server + globaler Agent)
# - Auto-Erkennung von Nodes/Swarm-Status
# - Automatische Einrichtung/Verbindung der Portainer-Agents auf allen Swarm-Nodes
# - Idempotent & vorsichtig (kein swarm leave / stack rm / volume delete)
#
# Ausführung: bevorzugt auf dem vorgesehenen Swarm-Manager (z.B. pve04).
# Voraussetzungen (lokal): bash, ssh; docker/curl je nach Schritt.

if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------- Defaults (editierbar im Menü) ----------
MANAGER_NAME="pve04"
MANAGER_HOST="192.168.178.254"      # SSH/Reachability host for manager
ADVERTISE_ADDR="192.168.178.254"    # Swarm advertise addr
MANAGER_ROLE="core"

# Worker list entries: name|host|role
WORKERS=(
  "pve01|192.168.178.250|edge"
  "pve02|192.168.178.251|ai"
  "pve03|192.168.178.252|growth"
)

SSH_USER="root"
SSH_KEY=""
SSH_PORT="22"
SSH_EXTRA_OPTS=""

STACK_FILE="${ROOT_DIR}/stacks/swarm/portainer.stack.yml"
STACK_NAME="portainer"
PORTAINER_PORT="9443"

DRY_RUN=false
VERBOSE=false

# ---------- UI helpers ----------
if [[ -t 1 ]]; then
  C_RST=$'\033[0m'; C_B=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_BLU=$'\033[34m'; C_CYN=$'\033[36m'
else
  C_RST=""; C_B=""; C_DIM=""; C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_CYN=""
fi

info() { printf '%s[INFO]%s %s\n' "$C_CYN" "$C_RST" "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$C_YLW" "$C_RST" "$*"; }
err()  { printf '%s[ERR ]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
dbg()  { $VERBOSE && printf '%s[DBG ]%s %s\n' "$C_DIM" "$C_RST" "$*" || true; }

pause() { printf '\n%sDrücke Enter zum Fortfahren...%s' "$C_DIM" "$C_RST"; read -r _ || true; }
hr() { printf '%s\n' "--------------------------------------------------------------------------"; }

confirm() {
  local prompt="${1:-Fortfahren?}" def="${2:-n}" ans
  local hint="[y/N]"; [[ "$def" == "y" ]] && hint="[Y/n]"
  read -r -p "$prompt $hint " ans || ans=""
  ans="${ans:-$def}"
  [[ "$ans" =~ ^[YyJj]$ ]]
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Benötigtes Kommando fehlt: $1"; return 1; }
}

# ---------- SSH/run helpers ----------
ssh_base_opts() {
  local opts=(-p "$SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)
  [[ -n "$SSH_KEY" ]] && opts+=(-i "$SSH_KEY")
  # shellcheck disable=SC2206
  [[ -n "$SSH_EXTRA_OPTS" ]] && opts+=($SSH_EXTRA_OPTS)
  printf '%s\n' "${opts[@]}"
}

ssh_cmd() {
  # usage: ssh_cmd <host> <command...>
  local host="$1"; shift
  local -a opts
  mapfile -t opts < <(ssh_base_opts)
  ssh "${opts[@]}" "${SSH_USER}@${host}" "$@"
}

is_local_host() {
  local host="$1"
  [[ "$host" == "localhost" || "$host" == "127.0.0.1" ]] && return 0
  # treat as local if matches a local IP/hostname
  local hn shn
  hn="$(hostname 2>/dev/null || true)"; shn="$(hostname -s 2>/dev/null || true)"
  [[ -n "$hn" && "$host" == "$hn" ]] && return 0
  [[ -n "$shn" && "$host" == "$shn" ]] && return 0
  [[ -n "$MANAGER_HOST" && "$host" == "$MANAGER_HOST" && "$host" == "$(hostname -I 2>/dev/null | awk '{print $1}')" ]] && return 0
  return 1
}

run_on() {
  # usage: run_on <host> <command-string>
  local host="$1"; shift
  local cmd="$*"
  if is_local_host "$host"; then
    dbg "LOCAL[$host]: $cmd"
    $DRY_RUN && { echo "+ (local) $cmd"; return 0; }
    bash -lc "$cmd"
  else
    dbg "SSH[$SSH_USER@$host]: $cmd"
    $DRY_RUN && { echo "+ (ssh $host) $cmd"; return 0; }
    ssh_cmd "$host" "bash -lc $(printf '%q' "$cmd")"
  fi
}

run_local() {
  local cmd="$*"
  dbg "LOCAL: $cmd"
  $DRY_RUN && { echo "+ $cmd"; return 0; }
  bash -lc "$cmd"
}

# ---------- Node/model helpers ----------
all_node_names() { printf '%s\n' "$MANAGER_NAME"; for w in "${WORKERS[@]}"; do IFS='|' read -r n _ _ <<<"$w"; printf '%s\n' "$n"; done; }
get_worker_field() { # idx field(1=name,2=host,3=role)
  local idx="$1" f="$2"; IFS='|' read -r n h r <<<"${WORKERS[$idx]}"; case "$f" in 1) echo "$n";; 2) echo "$h";; 3) echo "$r";; esac; }
node_host() {
  local name="$1"
  if [[ "$name" == "$MANAGER_NAME" ]]; then echo "$MANAGER_HOST"; return 0; fi
  for w in "${WORKERS[@]}"; do IFS='|' read -r n h r <<<"$w"; [[ "$n" == "$name" ]] && { echo "$h"; return 0; }; done
  return 1
}
node_role() {
  local name="$1"
  if [[ "$name" == "$MANAGER_NAME" ]]; then echo "$MANAGER_ROLE"; return 0; fi
  for w in "${WORKERS[@]}"; do IFS='|' read -r n h r <<<"$w"; [[ "$n" == "$name" ]] && { echo "$r"; return 0; }; done
  echo "worker"
}

# ---------- Status/Detect ----------
swarm_local_state() { # host -> inactive|active|unknown|no-docker|unreachable
  local host="$1" out
  if ! out=$(run_on "$host" "command -v docker >/dev/null 2>&1 || echo NO_DOCKER" 2>&1); then echo "unreachable"; return 0; fi
  [[ "$out" == *NO_DOCKER* ]] && { echo "no-docker"; return 0; }
  out=$(run_on "$host" "docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || echo unknown" 2>/dev/null || echo "unknown")
  echo "${out:-unknown}"
}

manager_is_swarm() { [[ "$(swarm_local_state "$MANAGER_HOST")" == "active" ]]; }
node_in_swarm() { # by hostname/id match in manager's node ls
  local name="$1"
  manager_is_swarm || return 1
  run_local "docker node ls --format '{{.Hostname}} {{.ID}} {{.Status}} {{.ManagerStatus}}' 2>/dev/null | awk '{print \$1}' | grep -qx $(printf '%q' "$name")" >/dev/null 2>&1
}

show_config() {
  hr
  printf '%sAktuelle Konfiguration%s\n' "$C_B" "$C_RST"
  echo "  Manager : $MANAGER_NAME @ $MANAGER_HOST (advertise: $ADVERTISE_ADDR, role: $MANAGER_ROLE)"
  echo "  Worker  :"
  for w in "${WORKERS[@]}"; do IFS='|' read -r n h r <<<"$w"; echo "    - $n @ $h (role: $r)"; done
  echo "  SSH     : user=$SSH_USER port=$SSH_PORT key=${SSH_KEY:-<default>} extra='${SSH_EXTRA_OPTS}'"
  echo "  Stack   : $STACK_NAME <- $STACK_FILE"
  echo "  Portainer: https://${MANAGER_HOST}:${PORTAINER_PORT}"
  echo "  Flags   : dry-run=$DRY_RUN verbose=$VERBOSE"
  hr
}

# ---------- Actions ----------
action_preflight() {
  need_cmd ssh || return 1
  hr; info "Preflight / Auto-Erkennung"; hr
  local all_ok=true
  # manager
  for name in $(all_node_names); do
    local host role state docker_v hn
    host="$(node_host "$name")"; role="$(node_role "$name")"
    printf '%s== %s (%s, role=%s) ==%s\n' "$C_B" "$name" "$host" "$role" "$C_RST"
    if is_local_host "$host"; then
      ok "Host: lokal"
    else
      if ssh_cmd "$host" "echo ok" >/dev/null 2>&1; then ok "SSH erreichbar (${SSH_USER}@${host})"; else err "SSH NICHT erreichbar (${SSH_USER}@${host})"; all_ok=false; printf '\n'; continue; fi
    fi
    if run_on "$host" "command -v docker >/dev/null 2>&1" >/dev/null 2>&1; then
      docker_v=$(run_on "$host" "docker --version 2>/dev/null | head -1" 2>/dev/null || echo "?")
      ok "Docker: $docker_v"
      state=$(swarm_local_state "$host")
      echo "  Swarm LocalNodeState: $state"
      hn=$(run_on "$host" "hostname -s 2>/dev/null || hostname 2>/dev/null || echo ?" 2>/dev/null || echo "?")
      echo "  Hostname: $hn"
    else
      warn "Docker: nicht installiert (Schritt 'Docker installieren/prüfen' nutzen)"
      all_ok=false
    fi
    printf '\n'
  done
  # swarm cluster view from manager
  if manager_is_swarm; then
    info "Swarm-Cluster (vom Manager):"
    run_local "docker node ls 2>/dev/null || true"
    echo
    info "Portainer-Stack (falls vorhanden):"
    run_local "docker stack services ${STACK_NAME} 2>/dev/null || echo '  (kein Stack ${STACK_NAME})'"
    run_local "docker stack ps ${STACK_NAME} --no-trunc 2>/dev/null | head -20 || true"
  else
    warn "Manager ist (noch) kein aktiver Swarm-Node."
  fi
  $all_ok && ok "Preflight grundlegend ok" || warn "Preflight: Probleme erkannt (siehe oben)"
}

configure_targets() {
  while true; do
    clear 2>/dev/null || true
    show_config
    cat <<'MENU'
[Konfiguration]
  1) Manager setzen (Name, Host/IP, Advertise, Rolle)
  2) Worker hinzufügen/bearbeiten/entfernen
  3) SSH (User, Key, Port, Extra-Opts)
  4) Portainer Stack/Port (Stack-Datei, Name, Port)
  5) Standardwerte wiederherstellen (pve01-pve04)
  0) Zurück
MENU
    read -r -p "Auswahl: " c || c=0
    case "$c" in
      1)
        read -r -p "Manager Name [$MANAGER_NAME]: " v || v=""; MANAGER_NAME="${v:-$MANAGER_NAME}"
        read -r -p "Manager Host/IP [$MANAGER_HOST]: " v || v=""; MANAGER_HOST="${v:-$MANAGER_HOST}"
        read -r -p "Advertise Addr [$ADVERTISE_ADDR]: " v || v=""; ADVERTISE_ADDR="${v:-$ADVERTISE_ADDR}"
        read -r -p "Manager Rolle [$MANAGER_ROLE]: " v || v=""; MANAGER_ROLE="${v:-$MANAGER_ROLE}"
        ;;
      2) configure_workers_menu ;;
      3)
        read -r -p "SSH User [$SSH_USER]: " v || v=""; SSH_USER="${v:-$SSH_USER}"
        read -r -p "SSH Key-Pfad [${SSH_KEY:-leer}]: " v || v=""; SSH_KEY="${v:-$SSH_KEY}"
        read -r -p "SSH Port [$SSH_PORT]: " v || v=""; SSH_PORT="${v:-$SSH_PORT}"
        read -r -p "SSH Extra-Opts [${SSH_EXTRA_OPTS}]: " v || v=""; SSH_EXTRA_OPTS="${v:-$SSH_EXTRA_OPTS}"
        ;;
      4)
        read -r -p "Stack-Datei [$STACK_FILE]: " v || v=""; STACK_FILE="${v:-$STACK_FILE}"
        read -r -p "Stack-Name [$STACK_NAME]: " v || v=""; STACK_NAME="${v:-$STACK_NAME}"
        read -r -p "Portainer HTTPS Port [$PORTAINER_PORT]: " v || v=""; PORTAINER_PORT="${v:-$PORTAINER_PORT}"
        ;;
      5)
        MANAGER_NAME="pve04"; MANAGER_HOST="192.168.178.254"; ADVERTISE_ADDR="192.168.178.254"; MANAGER_ROLE="core"
        WORKERS=("pve01|192.168.178.250|edge" "pve02|192.168.178.251|ai" "pve03|192.168.178.252|growth")
        SSH_USER="root"; SSH_KEY=""; SSH_PORT="22"; SSH_EXTRA_OPTS=""
        STACK_FILE="${ROOT_DIR}/stacks/swarm/portainer.stack.yml"; STACK_NAME="portainer"; PORTAINER_PORT="9443"
        ok "Defaults wiederhergestellt."
        sleep 1
        ;;
      0) break ;;
    esac
  done
}

configure_workers_menu() {
  while true; do
    echo; info "Worker-Nodes:"; local i=0
    for w in "${WORKERS[@]}"; do IFS='|' read -r n h r <<<"$w"; echo "  [$i] $n @ $h (role=$r)"; i=$((i+1)); done
    echo "  [a] hinzufügen  [d] löschen  [e] bearbeiten  [0] zurück"
    read -r -p "Auswahl: " c || c=0
    case "$c" in
      a)
        read -r -p "Name: " n || n=""; read -r -p "Host/IP: " h || h=""; read -r -p "Rolle [worker]: " r || r="worker"
        [[ -n "$n" && -n "$h" ]] && WORKERS+=("${n}|${h}|${r:-worker}") || warn "Name/Host erforderlich"
        ;;
      d)
        read -r -p "Index löschen: " idx || idx=""; if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx < ${#WORKERS[@]} )); then unset 'WORKERS[idx]'; WORKERS=("${WORKERS[@]}"); else warn "Ungültig"; fi
        ;;
      e)
        read -r -p "Index bearbeiten: " idx || idx="";
        if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx < ${#WORKERS[@]} )); then
          IFS='|' read -r n h r <<<"${WORKERS[$idx]}"
          read -r -p "Name [$n]: " v||v=""; n="${v:-$n}"; read -r -p "Host [$h]: " v||v=""; h="${v:-$h}"; read -r -p "Rolle [$r]: " v||v=""; r="${v:-$r}"
          WORKERS[$idx]="${n}|${h}|${r}"
        else warn "Ungültig"; fi
        ;;
      0) break ;;
    esac
  done
}

action_install_docker() {
  info "Docker-Installation/Prüfung (opt-in, idempotent)"
  warn "Installiert Docker nur auf Nodes ohne Docker (erfordert Root/Sudo & Internet)."
  confirm "Docker auf fehlenden Nodes installieren?" "n" || { info "Abgebrochen."; return 0; }
  for name in $(all_node_names); do
    local host; host="$(node_host "$name")"
    echo; info "Node $name ($host)"
    if run_on "$host" "command -v docker >/dev/null 2>&1" >/dev/null 2>&1; then
      ok "Docker bereits installiert: $(run_on "$host" 'docker --version 2>/dev/null | head -1' 2>/dev/null || echo present)"
      continue
    fi
    warn "Docker fehlt auf $name - Installation starten?"
    if ! confirm "  Jetzt installieren?" "n"; then info "  Übersprungen."; continue; fi
    info "  Installiere Docker (get.docker.com, fallback apt)..."
    run_on "$host" 'set -e; if command -v curl >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sh; elif command -v wget >/dev/null 2>&1; then wget -qO- https://get.docker.com | sh; elif command -v apt-get >/dev/null 2>&1; then apt-get update -y && apt-get install -y docker.io; else echo "Kein unterstützter Installer gefunden"; exit 1; fi; systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true'
    if run_on "$host" "command -v docker >/dev/null 2>&1" >/dev/null 2>&1; then ok "Docker installiert auf $name"; else err "Docker-Installation auf $name fehlgeschlagen"; fi
  done
}

action_swarm_setup() {
  need_cmd docker || { err "Docker lokal (Manager) benötigt."; return 1; }
  info "Swarm initialisieren & Worker joinen (idempotent)"

  # init manager
  local mstate; mstate=$(swarm_local_state "$MANAGER_HOST")
  if [[ "$mstate" == "active" ]]; then
    ok "Manager $MANAGER_NAME bereits im Swarm aktiv."
  elif [[ "$mstate" == "inactive" ]]; then
    info "Initialisiere Swarm auf Manager ($MANAGER_HOST), advertise=$ADVERTISE_ADDR"
    run_on "$MANAGER_HOST" "docker swarm init --advertise-addr $(printf '%q' "$ADVERTISE_ADDR")"
    ok "Swarm initialisiert."
  else
    err "Manager-Status: $mstate (erwartet: Docker installiert & erreichbar). Abbruch."
    return 1
  fi

  # get worker token (run on manager context; if remote manager host and we run elsewhere, fetch via ssh)
  local wtoken
  if is_local_host "$MANAGER_HOST"; then
    wtoken=$($DRY_RUN && echo "<dry-run-token>" || docker swarm join-token -q worker)
  else
    wtoken=$(run_on "$MANAGER_HOST" "docker swarm join-token -q worker" 2>/dev/null || echo "")
  fi
  if [[ -z "$wtoken" ]]; then err "Konnte Worker-Join-Token nicht ermitteln."; return 1; fi
  dbg "Worker token erhalten."

  # join workers
  for w in "${WORKERS[@]}"; do
    IFS='|' read -r n h r <<<"$w"
    echo; info "Worker $n ($h)"
    local st; st=$(swarm_local_state "$h")
    case "$st" in
      active)
        ok "$n ist bereits Swarm-Mitglied (auto-erkannt)."
        ;;
      inactive)
        info "Joine $n dem Swarm..."
        run_on "$h" "docker swarm join --token $(printf '%q' "$wtoken") $(printf '%q' "${ADVERTISE_ADDR}:2377")"
        ok "$n gejoint."
        ;;
      no-docker) err "$n: Docker fehlt (erst installieren)." ;;
      unreachable) err "$n: nicht erreichbar per SSH." ;;
      *) warn "$n: Status '$st' - Join übersprungen." ;;
    esac
  done

  echo; info "Cluster-Knoten (aktuell):"
  run_local "docker node ls 2>/dev/null || true"
}

resolve_swarm_node_id() {
  # best-effort: match by hostname (swarm node hostname) == given name
  local name="$1"
  run_local "docker node ls --format '{{.Hostname}} {{.ID}}' 2>/dev/null | awk -v n=$(printf '%q' "$name") '\$1==n{print \$2; exit}'" 2>/dev/null || true
}

action_label_nodes() {
  manager_is_swarm || { err "Kein aktiver Swarm auf Manager. Erst 'Swarm initialisieren/joinen'."; return 1; }
  info "Nodes labeln (omni.role / omni.zone) - auto-matching nach Hostname"
  run_local "docker node ls" || true
  echo
  for name in $(all_node_names); do
    local role nid; role="$(node_role "$name")"
    nid="$(resolve_swarm_node_id "$name")"
    if [[ -z "$nid" ]]; then
      warn "Kein Swarm-Node mit Hostname '$name' gefunden (Hostname in 'docker node ls' prüfen). Übersprungen."
      continue
    fi
    info "Label $name ($nid): omni.role=$role omni.zone=$name"
    run_local "docker node update --label-add omni.role=$(printf '%q' "$role") --label-add omni.zone=$(printf '%q' "$name") $(printf '%q' "$nid") >/dev/null"
    ok "$name gelabelt."
  done
  echo; info "Labels (Ist):"
  run_local "docker node ls -q 2>/dev/null | xargs -I{} sh -c 'echo --- {}; docker node inspect {} --format \"{{.Description.Hostname}} {{json .Spec.Labels}}\"' 2>/dev/null || true"
}

action_deploy_portainer() {
  manager_is_swarm || { err "Kein aktiver Swarm auf Manager."; return 1; }
  [[ -f "$STACK_FILE" ]] || { err "Stack-Datei nicht gefunden: $STACK_FILE"; return 1; }
  info "Deploy Portainer Stack: $STACK_NAME ($STACK_FILE)"
  info "Hinweis: Agent läuft global -> automatisch auf allen Linux-Nodes, Portainer verbindet via tasks.agent (auto)."
  run_local "docker stack deploy -c $(printf '%q' "$STACK_FILE") $(printf '%q' "$STACK_NAME")"
  ok "Stack deploy ausgelöst."
}

action_wait_verify() {
  manager_is_swarm || { err "Kein aktiver Swarm auf Manager."; return 1; }
  need_cmd docker || return 1
  info "Warte & verifiziere: Nodes, Agenten (global) und Portainer"
  local timeout=180 interval=5 elapsed=0
  while (( elapsed < timeout )); do
    local ready_nodes desired_agents running_agents portainer_ok
    ready_nodes=$(docker node ls --format '{{.Status}} {{.Availability}}' 2>/dev/null | awk '$1=="Ready" && $2=="Active"{c++} END{print c+0}')
    desired_agents=$(docker service ls --filter "label=com.docker.stack.namespace=${STACK_NAME}" --format '{{.Name}} {{.Mode}} {{.Replicas}}' 2>/dev/null | awk '/agent/{print $3}' | awk -F'/' '{print $2+0; exit}')
    running_agents=$(docker service ls --filter "label=com.docker.stack.namespace=${STACK_NAME}" --format '{{.Name}} {{.Mode}} {{.Replicas}}' 2>/dev/null | awk '/agent/{print $3}' | awk -F'/' '{print $1+0; exit}')
    portainer_ok=$(docker service ls --filter "label=com.docker.stack.namespace=${STACK_NAME}" --format '{{.Name}} {{.Replicas}}' 2>/dev/null | awk '/portainer/{print $2}' || true)

    printf '\r  [%3ds] ReadyNodes=%s  Agenten=%s/%s  Portainer=%s   ' "$elapsed" "${ready_nodes:-?}" "${running_agents:-?}" "${desired_agents:-?}" "${portainer_ok:-?}"

    # success: agents running == desired == ready_nodes (>=1) and portainer 1/1
    if [[ "${ready_nodes:-0}" -ge 1 && "${desired_agents:-0}" -ge 1 && "${running_agents:-0}" == "${desired_agents:-0}" && "${running_agents:-0}" == "${ready_nodes:-0}" && "${portainer_ok}" == 1/1* ]]; then
      echo; ok "Alle Agenten laufen auf allen Ready-Nodes und Portainer ist bereit (automatische Verbindung aktiv)."
      break
    fi
    sleep "$interval"; elapsed=$((elapsed+interval))
  done
  (( elapsed >= timeout )) && { echo; warn "Timeout beim Warten - aktueller Status unten (ggf. Images pull/Start verzögert)."; }

  echo; info "docker node ls:"; docker node ls || true
  echo; info "Stack Services:"; docker stack services "$STACK_NAME" || true
  echo; info "Stack Tasks:"; docker stack ps "$STACK_NAME" --no-trunc || true

  # API reachability
  echo; info "Portainer API-Erreichbarkeit:"
  if command -v curl >/dev/null 2>&1; then
    if curl -sk --max-time 5 "https://${MANAGER_HOST}:${PORTAINER_PORT}/api/status" >/dev/null 2>&1; then
      ok "Portainer erreichbar: https://${MANAGER_HOST}:${PORTAINER_PORT}"
    else
      warn "Portainer API noch nicht erreichbar unter https://${MANAGER_HOST}:${PORTAINER_PORT} (Start kann dauern / Port/Firewall prüfen)."
    fi
  else
    warn "curl fehlt lokal - API-Check übersprungen. UI: https://${MANAGER_HOST}:${PORTAINER_PORT}"
  fi
}

action_bootstrap_admin() {
  need_cmd curl || { err "curl benötigt."; return 1; }
  local base="https://${MANAGER_HOST}:${PORTAINER_PORT}"
  info "Portainer Admin-Bootstrap (optional, nur wenn noch nicht initialisiert)"
  echo "  Ziel: $base"
  # check initialized: /api/users/admin/check -> 204 if exists, 404/other if not; or status
  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "${base}/api/users/admin/check" 2>/dev/null || echo "000")
  if [[ "$code" == "204" ]]; then
    ok "Admin-Benutzer existiert bereits (initialisiert). Kein Eingriff."
    return 0
  fi
  info "Admin scheint nicht initialisiert (HTTP $code). Initialisierung möglich."
  if ! confirm "Admin jetzt automatisch anlegen?" "n"; then info "Abgebrochen (manuell im UI möglich)."; return 0; fi
  local user pass1 pass2
  read -r -p "Admin Username [admin]: " user || user=""; user="${user:-admin}"
  read -r -s -p "Admin Passwort: " pass1 || pass1=""; echo
  read -r -s -p "Passwort wiederholen: " pass2 || pass2=""; echo
  if [[ -z "$pass1" || "$pass1" != "$pass2" ]]; then err "Passwörter leer oder ungleich."; return 1; fi
  if (( ${#pass1} < 12 )); then warn "Passwort < 12 Zeichen (Portainer-Anforderung je Version beachten)."; confirm "Trotzdem senden?" "n" || return 1; fi
  local resp http
  http=$(curl -sk -o /tmp/portainer_init_resp.$$ -w '%{http_code}' --max-time 15 \
    -H 'Content-Type: application/json' \
    -X POST "${base}/api/users/admin/init" \
    -d "{\"Username\":\"${user//\"/\\\"}\",\"Password\":\"${pass1//\"/\\\"}\"}" 2>/dev/null || echo "000")
  resp="$(cat /tmp/portainer_init_resp.$$ 2>/dev/null || true)"; rm -f /tmp/portainer_init_resp.$$ 2>/dev/null || true
  if [[ "$http" == "200" || "$http" == "201" ]]; then
    ok "Admin initialisiert. Login: ${base} (User: $user)"
  elif [[ "$http" == "409" ]]; then
    ok "Admin bereits initialisiert (409)."
  else
    err "Initialisierung fehlgeschlagen (HTTP $http). Antwort: ${resp:0:200}"
    warn "Bitte manuell unter ${base} einrichten."
    return 1
  fi
}

action_full_setup() {
  info "Komplett-Setup (geführt): Preflight -> [Docker] -> Swarm -> Labels -> Portainer -> Verify -> [Admin]"
  show_config
  confirm "Mit dieser Konfiguration starten?" "y" || return 0
  action_preflight || warn "Preflight meldete Probleme."
  confirm "Weiter mit Swarm-Setup?" "y" || return 0
  if confirm "Fehlendes Docker automatisch installieren (falls nötig)?" "n"; then action_install_docker; fi
  action_swarm_setup || { err "Swarm-Setup fehlgeschlagen."; return 1; }
  action_label_nodes || warn "Labeling unvollständig."
  action_deploy_portainer || { err "Portainer-Deploy fehlgeschlagen."; return 1; }
  action_wait_verify
  if confirm "Portainer-Admin jetzt automatisch initialisieren?" "n"; then action_bootstrap_admin; fi
  echo; ok "Komplett-Setup abgeschlossen. UI: https://${MANAGER_HOST}:${PORTAINER_PORT}"
  info "Agenten werden global auf allen Swarm-Nodes ausgeführt und automatisch verbunden."
}

toggle_dry_run() { $DRY_RUN && DRY_RUN=false || DRY_RUN=true; info "Dry-Run: $DRY_RUN"; }
toggle_verbose() { $VERBOSE && VERBOSE=false || VERBOSE=true; info "Verbose: $VERBOSE"; }

main_menu() {
  while true; do
    clear 2>/dev/null || true
    cat <<BANNER
${C_B}============================================================${C_RST}
${C_B}  Portainer + Docker Swarm Setup (interaktiv, auto-agents) ${C_RST}
${C_B}============================================================${C_RST}
  Manager: ${MANAGER_NAME} (${MANAGER_HOST})   Stack: ${STACK_NAME}
  Flags  : dry-run=${DRY_RUN}  verbose=${VERBOSE}
BANNER
    cat <<'MENU'
------------------------------------------------------------
  1) Inventar / Preflight (Auto-Erkennung Nodes & Status)
  2) Ziele & SSH konfigurieren
  3) Docker installieren/prüfen (opt-in, fehlende Nodes)
  4) Swarm initialisieren & Worker joinen (idempotent)
  5) Nodes labeln (omni.role/omni.zone)
  6) Portainer deployen (Server + globaler Agent, auto-connect)
  7) Warten & verifizieren (Agents auf allen Nodes?)
  8) Admin initialisieren (optional, API)
  9) KOMPLETT-SETUP (geführt, 1->7)
  d) Dry-Run umschalten
  v) Verbose umschalten
  s) Konfiguration anzeigen
  0) Beenden
------------------------------------------------------------
MENU
    read -r -p "Auswahl: " choice || choice=0
    echo
    case "$choice" in
      1) action_preflight; pause ;;
      2) configure_targets ;;
      3) action_install_docker; pause ;;
      4) action_swarm_setup; pause ;;
      5) action_label_nodes; pause ;;
      6) action_deploy_portainer; pause ;;
      7) action_wait_verify; pause ;;
      8) action_bootstrap_admin; pause ;;
      9) action_full_setup; pause ;;
      d|D) toggle_dry_run; sleep 1 ;;
      v|V) toggle_verbose; sleep 1 ;;
      s|S) show_config; pause ;;
      0|q|Q) info "Beende."; exit 0 ;;
      *) warn "Ungültige Auswahl."; sleep 1 ;;
    esac
  done
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run] [--verbose] [--help]
  Interaktives, menü-geführtes Setup: Docker Swarm + Portainer (Agent global, auto-connect).
  Ohne Argumente startet das Menü. Ausführung bevorzugt auf dem Swarm-Manager.
EOF
}

# ---------- CLI ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --verbose|-v) VERBOSE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) warn "Unbekanntes Argument: $1"; usage; exit 2 ;;
  esac
done

main_menu
