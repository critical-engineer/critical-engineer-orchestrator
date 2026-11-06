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


def test_firewall_blocks_the_signal_api_and_the_home_network_for_the_worker():
    nft = (DEPLOY / "ceo-worker.nft").read_text()
    blocking = [line for line in rules(DEPLOY / "ceo-worker.nft") if "reject" in line]
    assert blocking and all('meta skuid "ceo-worker"' in line for line in blocking)
    local_ports = re.search(r"127\.0\.0\.0/8 tcp dport \{([^}]*)\}", nft).group(1)
    assert {"9922", "8096", "22"} <= {p.strip() for p in local_ports.split(",")}
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"):
        assert cidr in nft
    assert "policy accept" in nft  # only the worker's traffic is filtered


def test_setup_script_checks_before_installing_and_verifies_after():
    script = SCRIPT.read_text()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert script.index("visudo -cf /opt/ceo/deploy/sudoers-ceo") < script.index("/etc/sudoers.d/ceo")
    assert script.index("nft -c -f") < script.index("/etc/ceo/ceo-worker.nft")
    assert 'id -u)" -eq 0' in script  # refuses to run without root
    assert "set -euo pipefail" in script
    for check in ("worker can't read CEO's settings", "worker has no sudo", "worker can't reach the Signal API",
                  "worker can't reach the home network", "worker can't read any home directory"):
        assert check in script
    assert "chown -R root:root /opt/ceo" in script  # neither user can change CEO's code
    assert "chmod 0640 /etc/ceo/env" in script
