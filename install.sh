#!/bin/bash
# swarm installer. Run as root; idempotent.
#   install.sh coordinator [--cpu-limit N] [--user NAME]
#   install.sh worker <coord-ip> [--link-mbps N]
#   install.sh update            (re-run with the saved role/args on the latest main)
# Piped (curl ... | sudo bash -s -- <role> ...) it fetches main itself.
set -euo pipefail
# root's tools (runuser, useradd) live in sbin; `su` without `-` keeps a user PATH that lacks it
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

usage() { echo "usage: $0 coordinator [--cpu-limit N] [--user NAME] | worker <coord-ip> [--link-mbps N] | update" >&2; exit 2; }
die() { echo "install.sh: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"
SRC_URL=${SWARM_SRC_URL:-https://github.com/VocanicZ/swarm/archive/refs/heads/main.tar.gz}
ARGS=/etc/swarm/install.args
if [[ ${1:-} == update || -z ${BASH_SOURCE[0]:-} ]]; then
  if [[ ${1:-} == update ]]; then
    [[ -f $ARGS ]] || die "nothing to update: $ARGS missing (install first)"
    mapfile -t saved < "$ARGS"
    set -- "${saved[@]}"
  fi
  src=$(mktemp -d /var/tmp/swarm-src.XXXXXX)
  chmod 755 "$src"  # mktemp makes it 0700; rsync would copy that onto /opt/swarm
  curl -fsSL "$SRC_URL" | tar -xz -C "$src" --strip-components=1
  exec bash "$src/install.sh" "$@"
fi
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CFG=/etc/condor/config.d

role=${1:-}; shift || true
coord='' cpu_limit=$(nproc) link_mbps=100 user=${SUDO_USER:-}
case $role in
  coordinator) ;;
  worker) coord=${1:-}; [[ $coord =~ ^[A-Za-z0-9.:-]+$ ]] || usage; shift ;;
  *) usage ;;
esac
while (($#)); do
  case $1 in
    --cpu-limit) cpu_limit=${2:-}; shift 2 || usage ;;
    --link-mbps) link_mbps=${2:-}; shift 2 || usage ;;
    --user) user=${2:-}; shift 2 || usage ;;
    *) usage ;;
  esac
done
[[ $cpu_limit =~ ^[1-9][0-9]*$ && $link_mbps =~ ^[1-9][0-9]*$ ]] || usage

# --- common ---
if [[ $REPO != /opt/swarm ]]; then
  mkdir -p /opt/swarm
  command -v rsync >/dev/null || { apt-get update && apt-get install -y rsync; }  # absent on the claude LXC
  # root runs this code (tick, governor, `sudo install.sh` via join): root-owned, not group/other-writable
  rsync -a --delete --chown=root:root --chmod=go-w --exclude .git "$REPO/" /opt/swarm/
  chmod 755 /opt/swarm  # rsync copies the source dir mode, which may be 0700
fi
ln -sf /opt/swarm/bin/swarm /usr/local/bin/swarm
IP=$(ip -4 route get "${coord:-1.1.1.1}" | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
[[ -n $IP ]] || die "cannot find local IP toward ${coord:-1.1.1.1}"
# a worker pins its device, not its address: a new DHCP lease must not strand the startd
DEV=$(ip -4 route get "${coord:-1.1.1.1}" | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
[[ -n $DEV ]] || die "cannot find local device toward ${coord:-1.1.1.1}"

install_condor() {  # $@ = installer role flags
  command -v condor_version >/dev/null && return
  local pw=${GET_HTCONDOR_PASSWORD:-}
  [[ -n $pw ]] || read -rsp "HTCondor pool password: " pw </dev/tty
  echo
  curl -fsSL https://get.htcondor.org | GET_HTCONDOR_PASSWORD="$pw" bash -s -- --no-dry-run "$@"
}

if [[ $role == coordinator ]]; then
  install_condor --cm "$IP"
  mem_mb=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)
  sed -e "s/@IP@/$IP/" -e "s/@CPU_LIMIT@/$cpu_limit/" -e "s/@MEMORY_MB@/$mem_mb/" \
    /opt/swarm/condor/coordinator.config > "$CFG/99-swarm.config"
  rm -f "$CFG/99-swarm-spike.config"
  mkdir -p /etc/swarm
  printf 'CPU_LIMIT=%s\nCOORD_MACHINE=%s\n' "$cpu_limit" "$(condor_config_val FULL_HOSTNAME)" > /etc/swarm/coordinator.conf
  mkdir -p /etc/systemd/system/condor.service.d
  cp /opt/swarm/systemd/condor-idle.conf /etc/systemd/system/condor.service.d/
  cp /opt/swarm/systemd/swarm-tick.service /opt/swarm/systemd/swarm-tick.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now swarm-tick.timer
  systemctl restart condor
  if [[ -n $user ]]; then
    home=$(getent passwd "$user" | cut -d: -f6)
    [[ -n $home ]] || die "no such user: $user"
    # as the user: root writing into a user-owned tree would follow their symlinks
    # shellcheck disable=SC2016  # $HOME expands in the inner shell, as the user
    runuser -u "$user" -- env HOME="$home" sh -c \
      'mkdir -p "$HOME/.claude/skills/swarm" && cp -r /opt/swarm/skill/. "$HOME/.claude/skills/swarm/"'
  else
    echo "install.sh: no SUDO_USER or --user; skipping Claude Code skill" >&2
  fi
else
  install_condor --ep "$coord"
  id swarm >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin swarm
  mkdir -p /etc/swarm
  printf 'LINK_MBPS=%s\n' "$link_mbps" > /etc/swarm/worker.conf
  sed "s/@DEV@/$DEV/" /opt/swarm/condor/worker.config > "$CFG/99-swarm.config"
  rm -f "$CFG/99-swarm-spike.config"
  mkdir -p /etc/systemd/system/condor.service.d
  cp /opt/swarm/systemd/condor-instant-stop.conf /etc/systemd/system/condor.service.d/
  cp /opt/swarm/systemd/swarm-governor.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable swarm-governor
  systemctl restart swarm-governor  # restart, not start: picks up new code on re-install
  systemctl restart condor
fi
if [[ $role == coordinator ]]; then
  saved=(coordinator --cpu-limit "$cpu_limit")
  [[ -n $user ]] && saved+=(--user "$user")
else
  saved=(worker "$coord" --link-mbps "$link_mbps")
fi
printf '%s\n' "${saved[@]}" > "$ARGS"  # read back by `install.sh update` / `swarm update`
[[ $REPO == /var/tmp/swarm-src.* ]] && rm -rf "$REPO"
echo "swarm $role installed (IP $IP)"
