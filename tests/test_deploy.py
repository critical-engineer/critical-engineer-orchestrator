"""Security-relevant facts about the deploy files, so a careless edit fails CI."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEPLOY = ROOT / "deploy"
SCRIPT = ROOT / "scripts" / "setup-box.sh"


def rules(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.strip().startswith("#")]


def test_sudoers_lets_ceo_run_only_as_the_worker():
    lines = rules(DEPLOY / "sudoers-ceo")
    grants = [line for line in lines if not line.startswith("Defaults")]
    assert grants == ["ceo ALL=(ceo-worker) NOPASSWD: ALL"]
    assert not any(line.startswith("ceo-worker") for line in lines)  # the worker gets nothing
    assert not any("(ALL" in line or "(root" in line for line in lines)


def test_service_runs_as_ceo_without_access_to_home():
    unit = (DEPLOY / "ceo.service").read_text()
    assert "\nUser=ceo\n" in unit and "\nProtectHome=yes\n" in unit
    assert "Requires=ceo-firewall.service" in unit
    assert "ExecStart=/opt/ceo/.venv/bin/python -m ceo.run loop" in unit


def test_firewall_denies_the_worker_loopback_by_default_and_the_home_network():
    nft = (DEPLOY / "ceo-worker.nft").read_text()
    active = rules(DEPLOY / "ceo-worker.nft")
    worker_rules = [line for line in active if "accept" in line.split() or "reject" in line.split()]
    worker_rules = [line for line in worker_rules if not line.startswith("type ")]
    assert worker_rules and all('meta skuid "ceo-worker"' in line for line in worker_rules)
    # The loopback policy, exactly and in order: DNS, the ephemeral range, then
    # reject. Any extra allow rule (e.g. a blanket `oifname "lo" accept`) fails.
    loopback = [line for line in worker_rules if 'oifname "lo"' in line]
    assert loopback == [
        'meta skuid "ceo-worker" oifname "lo" meta l4proto { tcp, udp } th dport 53 accept',
        'meta skuid "ceo-worker" oifname "lo" meta l4proto { tcp, udp } th dport 32768-60999 accept',
        'meta skuid "ceo-worker" oifname "lo" counter reject',
    ]
    # And nothing else in the chain accepts traffic for the worker.
    assert [line for line in worker_rules if line.endswith("accept")] == loopback[:2]
    # Each private-network rule rejects exactly the expected ranges.
    private = {line for line in worker_rules if "daddr {" in line}
    assert private == {
        'meta skuid "ceo-worker" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10 } counter reject',
        'meta skuid "ceo-worker" ip6 daddr { fc00::/7, fe80::/10 } counter reject',
    }
    assert "policy accept" in nft  # only the worker's traffic is filtered


def test_the_shared_work_directory_cant_be_swapped_by_the_worker():
    script = SCRIPT.read_text()
    # /srv/ceo itself is root's, so the worker group can't rename or replace "work".
    assert "install -d -o root -g root -m 0755 /srv/ceo\n" in script
    assert "-g ceo-share -m 2770 /srv/ceo /srv/ceo/work" not in script
    # And a symlinked "work" is refused before root changes its owner or mode.
    assert script.index("[ -L /srv/ceo/work ]") < script.index("install -d -o ceo -g ceo-share -m 2770 /srv/ceo/work")


def test_setup_keeps_ceo_stopped_until_the_sandbox_checks_pass():
    script = SCRIPT.read_text()
    stop = script.index("systemctl stop ceo.service")
    checks = script.index('say "checking the sandbox from the worker\'s side"')
    cleanup = script.index("pkill -KILL -u ceo-worker")
    start = script.index("systemctl start ceo.service")
    assert stop < checks < cleanup < start
    assert "systemctl restart ceo.service" not in script


def test_setup_script_checks_before_installing_and_verifies_after():
    script = SCRIPT.read_text()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert script.index("visudo -cf /opt/ceo/deploy/sudoers-ceo") < script.index("/etc/sudoers.d/ceo")
    assert script.index("nft -c -f") < script.index("/etc/ceo/ceo-worker.nft")
    assert 'id -u)" -eq 0' in script  # refuses to run without root
    assert "set -euo pipefail" in script
    for check in ("worker can't read CEO's settings", "worker has no sudo", "worker can't reach the Signal API",
                  "worker can't reach the home network", "worker can't read any home directory",
                  "no worker process survives CEO's cleanup", "can run commands as the worker",
                  "couldn't start a test process as the worker"):
        assert check in script
    # Negative network checks demand curl's "couldn't connect" (7), not just any failure.
    assert 'expect_exit 7 "worker can\'t reach the Signal API' in script
    assert '|| got=$?' in script  # failures are reported, not fatal under set -e
    assert 'can\'t read $SEED_ENV' in script
    assert "chown -R root:root /opt/ceo" in script  # neither user can change CEO's code
    assert "chmod 0640 /etc/ceo/env" in script
