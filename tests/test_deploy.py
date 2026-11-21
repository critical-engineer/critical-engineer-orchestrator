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


def unit_settings(path: Path) -> dict[str, dict[str, list[str]]]:
    """Active settings of a systemd unit: {section: {key: [values]}}; comments ignored."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def test_service_runs_as_ceo_without_access_to_home():
    unit = unit_settings(DEPLOY / "ceo.service")
    assert unit["Unit"]["Requires"] == ["ceo-firewall.service"]
    assert unit["Service"]["User"] == ["ceo"]
    assert unit["Service"]["ProtectHome"] == ["yes"]
    assert unit["Service"]["ExecStart"] == ["/opt/ceo/.venv/bin/python -m ceo.run loop"]


def test_unit_parser_ignores_commented_settings(tmp_path):
    unit = tmp_path / "x.service"
    unit.write_text("[Unit]\n# Requires=ceo-firewall.service\n[Service]\nUser=ceo\n")
    assert "Requires" not in unit_settings(unit)["Unit"]


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
        'meta skuid "ceo-worker" oifname "lo" ip daddr { 127.0.0.53, 127.0.0.54 } meta l4proto { tcp, udp } th dport 53 accept',
        'meta skuid "ceo-worker" oifname "lo" meta l4proto { tcp, udp } th dport 32768-60999 accept',
        'meta skuid "ceo-worker" oifname "lo" counter reject',
    ]
    # And nothing else in the chain accepts traffic for the worker.
    assert [line for line in worker_rules if line.endswith("accept")] == loopback[:2]
    # Each private-network rule rejects exactly the expected ranges.
    private = {line for line in worker_rules if "daddr {" in line and 'oifname "lo"' not in line}
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
    # A stop that didn't take aborts before any check can kill a live task.
    still_running = script.index("if systemctl is-active --quiet ceo.service; then", stop)
    assert still_running < checks and "exit 1" in script[still_running:checks]


def shell_function(name: str) -> str:
    script = SCRIPT.read_text()
    match = re.search(rf"^{name}\(\) {{[^\n]*\n.*?^}}\n", script, re.S | re.M)
    assert match, name
    return match.group(0)


def run_helpers(tmp_path, body: str, stubs: dict[str, str]) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name, code in stubs.items():
        (bin_dir / name).write_text(f"#!/bin/bash\n{code}\n")
        (bin_dir / name).chmod(0o755)
    program = "\n".join([
        'ok() { echo "ok $*"; }', 'bad() { echo "FAIL $*"; }', 'as_worker() { WORKER=1 "$@"; }',
        shell_function("expect_exit"), shell_function("blocked_for_worker"), shell_function("is_worker_probe"), body,
    ])
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True, check=True).stdout


def test_a_network_block_only_passes_when_root_can_connect(tmp_path):
    # curl stub: root connects (0); the worker is refused (7).
    out = run_helpers(tmp_path, 'blocked_for_worker "blocked" http://x/',
                      {"curl": '[ -n "${WORKER:-}" ] && exit 7; exit 0'})
    assert out.strip() == "ok blocked"
    # Nothing listening at all: not a pass, a warning.
    out = run_helpers(tmp_path / "b", 'blocked_for_worker "blocked" http://x/', {"curl": "exit 7"})
    assert "WARN" in out and "ok" not in out
    # The worker gets through: a failure.
    out = run_helpers(tmp_path / "c", 'blocked_for_worker "blocked" http://x/', {"curl": "exit 0"})
    assert out.startswith("FAIL blocked")


def test_the_cleanup_probe_must_be_a_live_worker_sleep(tmp_path):
    for ps_output, alive in (("ceo-worker S sleep", True), ("ceo-worker Z sleep", False),
                             ("root S sleep", False), ("ceo-worker S bash", False), ("", False)):
        d = tmp_path / str(abs(hash(ps_output)))
        d.mkdir()
        out = run_helpers(d, "is_worker_probe 123 && echo alive || echo gone",
                          {"ps": f'echo "{ps_output}"; [ -n "{ps_output}" ]'})
        assert out.strip() == ("alive" if alive else "gone"), ps_output


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
    assert 'blocked_for_worker "worker can\'t reach the Signal API' in script
    assert 'expect_exit 7 "worker can\'t reach that local service"' in script  # against a real listener
    assert '|| got=$?' in script  # failures are reported, not fatal under set -e
    assert 'can\'t read $SEED_ENV' in script
    assert "chown -R root:root /opt/ceo" in script  # neither user can change CEO's code
    assert "chmod 0640 /etc/ceo/env" in script
