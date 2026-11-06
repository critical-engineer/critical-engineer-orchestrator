#!/usr/bin/env bash
# Set up CEO on the box (hosting PR; D5, C4, C5). Run as root; safe to re-run.
#
#   sudo bash setup-box.sh [--seed-env FILE] [--engineer-github LOGIN] [--ref REF]
#
# --seed-env FILE         copy CEO_CHANNEL / CEO_MATRIX_* settings from FILE
#                         (e.g. ~/.config/ceo/env) into /etc/ceo/env the first time
# --engineer-github LOGIN the engineer's GitHub login (approves plans by merging)
# --ref REF               git ref of CEO to install (default: main)
#
# What it does:
#   users      ceo (orchestrator), ceo-worker (agents and checks), shared group
#              ceo-share for the work directory only; no sudo/docker for either
#   sudoers    ceo may run commands as ceo-worker; nothing else
#   firewall   ceo-worker can't reach the home network or local services
#   code       /opt/ceo, owned by root (neither user can change it)
#   settings   /etc/ceo/env (root:ceo 0640); ledger in /var/lib/ceo
#   service    ceo.service runs `python -m ceo.run loop` as ceo
# and finishes by checking the sandbox from the worker's point of view.

set -euo pipefail

REPO_URL="https://github.com/critical-engineer/critical-engineer-orchestrator.git"
REPO_SLUG="critical-engineer/critical-engineer-orchestrator"
REF="main"
SEED_ENV=""
ENGINEER_GITHUB=""

while [ $# -gt 0 ]; do
  case "$1" in
    --seed-env) SEED_ENV="$2"; shift 2 ;;
    --engineer-github) ENGINEER_GITHUB="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0 ..." >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

say "users and groups"
getent group ceo-share >/dev/null || groupadd --system ceo-share
id ceo >/dev/null 2>&1 || useradd --system --user-group --create-home --home-dir /var/lib/ceo --shell /bin/bash ceo
id ceo-worker >/dev/null 2>&1 || useradd --system --user-group --create-home --home-dir /var/lib/ceo-worker --shell /bin/bash ceo-worker
usermod -aG ceo-share ceo
usermod -aG ceo-share ceo-worker
for user in ceo ceo-worker; do
  for group in sudo admin wheel docker adm lxd; do
    gpasswd -d "$user" "$group" >/dev/null 2>&1 || true
  done
  passwd -l "$user" >/dev/null
done
chmod 0750 /var/lib/ceo /var/lib/ceo-worker

say "directories"
install -d -o ceo -g ceo-share -m 2770 /srv/ceo /srv/ceo/work
install -d -o root -g ceo -m 0750 /etc/ceo

say "CEO code in /opt/ceo ($REF)"
if [ -d /opt/ceo/.git ]; then
  git -C /opt/ceo fetch -q origin
  git -C /opt/ceo checkout -q --detach "origin/$REF" 2>/dev/null || git -C /opt/ceo checkout -q --detach "$REF"
else
  git clone -q "$REPO_URL" /opt/ceo
  git -C /opt/ceo checkout -q --detach "$REF"
fi
[ -d /opt/ceo/.venv ] || python3 -m venv /opt/ceo/.venv
/opt/ceo/.venv/bin/pip install -q --upgrade /opt/ceo
chown -R root:root /opt/ceo
chmod -R go-w /opt/ceo

say "CEO's clone of the plan repo"
[ -d /var/lib/ceo/plans/.git ] || runuser -u ceo -- git clone -q "$REPO_URL" /var/lib/ceo/plans

say "opencode for the worker user"
if ! runuser -u ceo-worker -- test -x /var/lib/ceo-worker/.opencode/bin/opencode; then
  # The official installer, run as the worker into its own home.
  runuser -u ceo-worker -- env HOME=/var/lib/ceo-worker bash -c 'curl -fsSL https://opencode.ai/install | bash'
fi
runuser -u ceo-worker -- env HOME=/var/lib/ceo-worker /var/lib/ceo-worker/.opencode/bin/opencode --version

say "settings (/etc/ceo/env)"
if [ ! -f /etc/ceo/env ]; then
  {
    echo "# CEO settings. Secret: root:ceo 0640. Read by ceo.service."
    echo "CEO_LEDGER=/var/lib/ceo/ledger.db"
    echo "CEO_PLAN_REPO=$REPO_SLUG"
    echo "CEO_PLAN_REPO_DIR=/var/lib/ceo/plans"
    echo "# Comma-separated OpenSpec change names to run; empty = only read chat."
    echo "CEO_PLANS="
    echo "CEO_WORK_ROOT=/srv/ceo/work"
    echo "CEO_WORKER_USER=ceo-worker"
    echo "CEO_OPENCODE_BIN=/var/lib/ceo-worker/.opencode/bin/opencode"
    echo "CEO_ENGINEER_GITHUB=$ENGINEER_GITHUB"
    echo "CEO_BUDGET_USD=20"
    echo "CEO_INTERVAL=60"
    if [ -n "$SEED_ENV" ]; then
      grep -E '^(CEO_CHANNEL|CEO_MATRIX_[A-Z_]+|CEO_ENGINEER_CHAT)=' "$SEED_ENV" || true
    fi
  } > /etc/ceo/env
fi
chown root:ceo /etc/ceo/env
chmod 0640 /etc/ceo/env

say "sudoers: ceo may run commands as ceo-worker, nothing else"
visudo -cf /opt/ceo/deploy/sudoers-ceo >/dev/null
install -o root -g root -m 0440 /opt/ceo/deploy/sudoers-ceo /etc/sudoers.d/ceo
visudo -c >/dev/null

say "firewall for ceo-worker"
nft -c -f /opt/ceo/deploy/ceo-worker.nft
install -o root -g root -m 0644 /opt/ceo/deploy/ceo-worker.nft /etc/ceo/ceo-worker.nft
install -o root -g root -m 0644 /opt/ceo/deploy/ceo-firewall.service /etc/systemd/system/ceo-firewall.service
install -o root -g root -m 0644 /opt/ceo/deploy/ceo.service /etc/systemd/system/ceo.service
systemctl daemon-reload
systemctl enable --now ceo-firewall.service
systemctl restart ceo-firewall.service

say "service"
systemctl enable ceo.service
systemctl restart ceo.service

say "checking the sandbox from the worker's side"
fail=0
check() { if "$@"; then echo "  ok   $desc"; else echo "  FAIL $desc"; fail=1; fi; }
as_worker() { runuser -u ceo-worker -- "$@"; }

desc="worker can't read any home directory";  check bash -c 'for d in /home/* /root; do runuser -u ceo-worker -- test -r "$d" && exit 1; done; exit 0'
desc="worker can't read CEO's settings";      check bash -c '! runuser -u ceo-worker -- test -r /etc/ceo/env'
desc="worker can't read the ledger";          check bash -c '! runuser -u ceo-worker -- test -r /var/lib/ceo/ledger.db'
desc="worker has no sudo";                    check bash -c '! runuser -u ceo-worker -- sudo -n true 2>/dev/null'
desc="worker isn't in docker/sudo groups";    check bash -c '! id -nG ceo-worker | grep -qwE "docker|sudo|adm"'
desc="ceo can run as the worker";             check runuser -u ceo -- sudo -n -u ceo-worker true
desc="worker can't reach the Signal API";     check bash -c '! runuser -u ceo-worker -- curl -s -m 3 -o /dev/null http://127.0.0.1:9922/'
desc="worker can't reach the home network";   check bash -c '! runuser -u ceo-worker -- curl -s -m 3 -o /dev/null "http://$(ip route | awk "/default/ {print \$3; exit}")/"'
desc="worker can reach the internet";         check runuser -u ceo-worker -- curl -s -m 10 -o /dev/null https://opencode.ai/
desc="ceo.service is running";                check systemctl is-active --quiet ceo.service

if runuser -u ceo-worker -- test -r /media/admin/Data_Vault 2>/dev/null; then
  echo "  WARN worker can read /media/admin/Data_Vault (world-readable). C5 says it shouldn't;"
  echo "       not changed here because it could affect Jellyfin. Decide, e.g.: chmod o-rx /media/admin/Data_Vault"
fi

[ "$fail" -eq 0 ] && echo "All checks passed." || { echo "Some checks FAILED; see above."; exit 1; }
