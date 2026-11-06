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

For anything beyond trying it out, install a commit you've reviewed rather than a moving branch:
`--ref <commit sha>`.

## What you get

| | |
|---|---|
| `ceo` | Runs the orchestrator (`ceo.service`). Owns the ledger (`/var/lib/ceo/ledger.db`) and its clone of the plan repo (`/var/lib/ceo/plans`). |
| `ceo-worker` | Runs agents and checks. Has its own `opencode`. No sudo, no docker; can't read `/home`, CEO's settings or the ledger. |
| `ceo-share` | Group shared by both, for `/srv/ceo/work` only (per-task workspaces). |
| `/opt/ceo` | CEO's code, owned by root: neither user can change it. |
| `/etc/ceo/env` | Settings (root:ceo 0640), including the Matrix token. |
| sudoers | `ceo` may run commands as `ceo-worker`; nothing else. |
| firewall | `ceo-firewall.service` loads the `inet ceo_worker` table. For the worker, loopback is **denied by default** (no local service is reachable, whatever its port) except DNS and the kernel's ephemeral port range, where tests start their own servers; the home network is blocked; the internet stays open (the model API). |

## After setup

1. **Pick plans to run**: set `CEO_PLANS=<change>` in `/etc/ceo/env`, then
   `sudo systemctl restart ceo`. Empty means CEO only reads chat.
2. **Logs**: `journalctl -u ceo -f`.
3. **Worker model**: the default is OpenCode's free tier. For a paid model, configure the
   worker's opencode (`sudo -u ceo-worker -H /var/lib/ceo-worker/.opencode/bin/opencode auth login`)
   with a key that has its own credit limit (C3), and set `CEO_WORKER_MODEL`.
4. **The skill**: point its `authorized_keys` line (docs/skill.md) at `/opt/ceo/.venv/bin/python`
   and `--env-file /etc/ceo/env`, in `~ceo/.ssh/authorized_keys`.

## Known gaps

- `/media/admin/Data_Vault` is world-readable, so the worker can read it (C5 says it shouldn't).
  The script reports this rather than changing it, since it could affect Jellyfin.
- A local service listening on a port in the ephemeral range (32768-60999) would be reachable
  by the worker. Fixed services on the box listen below it.
