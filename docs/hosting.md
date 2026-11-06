# Hosting CEO on the box

`scripts/setup-box.sh` installs CEO as a service with the workers sandboxed (D5, C4, C5).
It runs as root, is safe to re-run, and ends by checking the sandbox from the worker's side.

## Run it

```sh
git clone https://github.com/critical-engineer/critical-engineer-orchestrator.git
sudo bash critical-engineer-orchestrator/scripts/setup-box.sh \
  --seed-env ~/.config/ceo/env \
  --engineer-github <your GitHub login>
```

`--seed-env` copies the chat settings (`CEO_CHANNEL`, `CEO_MATRIX_*`) into `/etc/ceo/env` the
first time. Read the script first: it creates users, a sudoers rule and firewall rules.

## What you get

| | |
|---|---|
| `ceo` | Runs the orchestrator (`ceo.service`). Owns the ledger (`/var/lib/ceo/ledger.db`) and its clone of the plan repo (`/var/lib/ceo/plans`). |
| `ceo-worker` | Runs agents and checks. Has its own `opencode`. No sudo, no docker; can't read `/home`, CEO's settings or the ledger. |
| `ceo-share` | Group shared by both, for `/srv/ceo/work` only (per-task workspaces). |
| `/opt/ceo` | CEO's code, owned by root: neither user can change it. |
| `/etc/ceo/env` | Settings (root:ceo 0640), including the Matrix token. |
| sudoers | `ceo` may run commands as `ceo-worker`; nothing else. |
| firewall | `ceo-firewall.service` loads the `inet ceo_worker` table: the worker can't reach the home network or local services (Signal API, Jellyfin, SSH, CUPS, port 8000). Its internet access and its own loopback traffic stay open. |

## After setup

1. **Pick plans to run**: set `CEO_PLANS=<change>` in `/etc/ceo/env`, then
   `sudo systemctl restart ceo`. Empty means CEO only reads chat.
2. **Logs**: `journalctl -u ceo -f`.
3. **Worker model**: the default is OpenCode's free tier. For a paid model, configure the
   worker's opencode (`sudo -u ceo-worker -H /var/lib/ceo-worker/.opencode/bin/opencode auth login`)
   with a key that has its own credit limit (C3), and set `CEO_WORKER_MODEL`.
4. **The skill**: point its `authorized_keys` line (docs/skill.md) at `/opt/ceo/.venv/bin/python`
   and `--env-file /etc/ceo/env`, in `~ceo/.ssh/authorized_keys`.

## Known gap

`/media/admin/Data_Vault` is world-readable, so the worker can read it (C5 says it shouldn't).
The script reports this rather than changing it, since it could affect Jellyfin.
