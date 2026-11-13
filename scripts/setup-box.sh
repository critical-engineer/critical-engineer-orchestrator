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
if [ -n "$SEED_ENV" ] && [ ! -r "$SEED_ENV" ]; then
  echo "--seed-env: can't read $SEED_ENV" >&2; exit 1
fi
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
# The dev extras (pytest) too: checks run with this venv first on PATH.
/opt/ceo/.venv/bin/pip install -q --upgrade "/opt/ceo[dev]"
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
ok()   { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; fail=1; }
# Run as the worker and return the command's own exit status (126/127 if the
# account switch itself fails, which every check below treats as a failure).
as_worker() { runuser -u ceo-worker -- "$@"; }
# A negative check only means something if the same mechanism works positively,
# so every "can't" below has a control that must succeed first.
expect_exit() {  # expect_exit <code> <description> <command...>
  local want=$1 desc=$2; shift 2
  local got=0
  "$@" >/dev/null 2>&1 || got=$?   # `|| got=$?` keeps `set -e` from aborting the script
  if [ "$got" -eq "$want" ]; then ok "$desc"; else bad "$desc (exit $got, expected $want)"; fi
}

if as_worker true; then ok "can run commands as the worker"; else bad "can't run commands as the worker"; fi
expect_exit 0 "worker can read its own home"            as_worker test -r /var/lib/ceo-worker
expect_exit 1 "worker can't read CEO's settings"        as_worker test -r /etc/ceo/env
expect_exit 1 "worker can't read CEO's home or ledger"  as_worker test -r /var/lib/ceo
if as_worker bash -c 'for d in /home/* /root; do [ -e "$d" ] || continue; test -r "$d" && { echo "$d"; exit 1; }; done; exit 0'
then ok "worker can't read any home directory"; else bad "worker can read a home directory (or the check couldn't run)"; fi
expect_exit 1 "worker has no sudo"                      as_worker sudo -n true
if id -nG ceo-worker | grep -qwE "docker|sudo|adm|lxd"; then bad "worker is in a privileged group"; else ok "worker isn't in docker/sudo/adm/lxd"; fi
expect_exit 0 "ceo can run as the worker"               runuser -u ceo -- sudo -n -u ceo-worker true
# The cleanup CEO runs after every task (Sandbox._clean_up): nothing survives it.
# Start a known process first and check it by PID, so the check can't pass
# because the process never started.
probe=$(runuser -u ceo -- sudo -n -u ceo-worker -- bash -c 'nohup sleep 300 >/dev/null 2>&1 & echo $!' 2>/dev/null || true)
if [ -n "$probe" ] && kill -0 "$probe" 2>/dev/null; then
  runuser -u ceo -- sudo -n -u ceo-worker -- pkill -KILL -u ceo-worker || true
  sleep 1
  if kill -0 "$probe" 2>/dev/null; then bad "a worker process survived CEO's cleanup (pid $probe)"
  else ok "no worker process survives CEO's cleanup"; fi
else
  bad "couldn't start a test process as the worker, so CEO's cleanup is untested"
fi
# Network: curl exit 7 means "couldn't connect" (refused or rejected), so a
# typo, a missing curl or a failed account switch can't pass these.
expect_exit 0 "worker can reach the internet"           as_worker curl -s -m 10 -o /dev/null https://opencode.ai/
expect_exit 7 "worker can't reach the Signal API (127.0.0.1:9922)" as_worker curl -s -m 3 -o /dev/null http://127.0.0.1:9922/
gateway=$(ip route | awk '/^default/ {print $3; exit}')
if [ -z "$gateway" ]; then
  bad "no default route found; couldn't check the home network"
else
  expect_exit 7 "worker can't reach the home network ($gateway)" as_worker curl -s -m 3 -o /dev/null "http://$gateway/"
fi
expect_exit 0 "checks can run pytest as the worker" as_worker env PATH=/opt/ceo/.venv/bin:/usr/bin:/bin python -m pytest --version
if systemctl is-active --quiet ceo.service; then ok "ceo.service is running"; else bad "ceo.service isn't running"; fi

if runuser -u ceo-worker -- test -r /media/admin/Data_Vault 2>/dev/null; then
  echo "  WARN worker can read /media/admin/Data_Vault (world-readable). C5 says it shouldn't;"
  echo "       not changed here because it could affect Jellyfin. Decide, e.g.: chmod o-rx /media/admin/Data_Vault"
fi

[ "$fail" -eq 0 ] && echo "All checks passed." || { echo "Some checks FAILED; see above."; exit 1; }
