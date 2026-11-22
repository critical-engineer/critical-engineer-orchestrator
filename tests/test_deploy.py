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
    }
    # No IPv6 beyond loopback: a home network can use a global IPv6 prefix.
    # It comes after the loopback rules, so ::1 still follows those.
    ipv6 = 'meta skuid "ceo-worker" meta nfproto ipv6 counter reject'
    assert ipv6 in worker_rules and worker_rules.index(ipv6) > worker_rules.index(loopback[-1])
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
    # Not enabled at boot until every check has passed.
    disable = script.index("systemctl disable ceo.service")
    enable = script.index("systemctl enable ceo.service")
    assert disable < script.index("if ! stop_ceo; then") and checks < enable < start
    assert script.count("systemctl enable ceo.service") == 1
    # A stop that didn't take aborts before any check can kill a live task.
    guard = script.index("if ! stop_ceo; then")
    assert stop < guard < checks and "exit 1" in script[guard:checks]


def test_ceo_counts_as_stopped_only_when_confirmed(tmp_path):
    cases = {
        "stopped": ("exit 0", "echo inactive; exit 3", True),
        "failed unit": ("exit 0", "echo failed; exit 3", True),
        "stop fails": ("exit 1", "echo inactive; exit 3", False),
        "still active": ("exit 0", "echo active; exit 0", False),
        "status unknown": ("exit 0", "exit 1", False),
    }
    for name, (stop, is_active, stopped) in cases.items():
        d = tmp_path / name.replace(" ", "-")
        stub = f'case "$1" in stop) {stop};; is-active) {is_active};; esac'
        out = run_helpers(d, "stop_ceo && echo stopped || echo running", {"systemctl": stub})
        assert out.strip() == ("stopped" if stopped else "running"), name


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
        shell_function("expect_exit"), shell_function("blocked_for_worker"), shell_function("is_worker_probe"),
        shell_function("stop_ceo"), body,
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
    # Extra curl options (e.g. -6) reach both root's control and the worker's attempt.
    out = run_helpers(tmp_path / "f", 'blocked_for_worker "v6" https://x/ -6',
                      {"curl": 'case "$*" in *-6*) ;; *) exit 99;; esac; [ -n "${WORKER:-}" ] && exit 7; exit 0'})
    assert out.strip() == "ok v6"
    # Root's own attempt fails some other way (6: couldn't resolve): not a pass.
    out = run_helpers(tmp_path / "d", 'blocked_for_worker "blocked" http://x/',
                      {"curl": '[ -n "${WORKER:-}" ] && exit 7; exit 6'})
    assert "WARN" in out and "exit 6" in out and "ok" not in out
    # Connected but empty reply (52) still proves root reached it.
    out = run_helpers(tmp_path / "e", 'blocked_for_worker "blocked" http://x/',
                      {"curl": '[ -n "${WORKER:-}" ] && exit 7; exit 52'})
    assert out.strip() == "ok blocked"
    # The worker gets through: a failure.
    out = run_helpers(tmp_path / "c", 'blocked_for_worker "blocked" http://x/', {"curl": "exit 0"})
    assert out.startswith("FAIL blocked")


def test_the_cleanup_probe_must_be_a_live_worker_sleep(tmp_path):
    for ps_output, alive in (("ceo-worker S sleep", True), ("ceo-worker Z sleep", False),
                             ("root S sleep", False), ("ceo-worker S bash", False), ("", False)):
        d = tmp_path / str(abs(hash(ps_output)))
        d.mkdir()
        # Like procps at its default width, the stub truncates long user names
        # unless asked for a wide column.
        truncate = '' if not ps_output.startswith("ceo-worker") else 'case "$*" in *user:32=*) ;; *) echo "ceo-wor+ ${OUT#* }"; exit 0;; esac;'
        out = run_helpers(d, "is_worker_probe 123 && echo alive || echo gone",
                          {"ps": f'OUT="{ps_output}"; {truncate} echo "$OUT"; [ -n "$OUT" ]'})
        assert out.strip() == ("alive" if alive else "gone"), ps_output


def test_setup_script_checks_before_installing_and_verifies_after():
    script = SCRIPT.read_text()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert script.index("visudo -cf /opt/ceo/deploy/sudoers-ceo") < script.index("/etc/sudoers.d/ceo")
    assert script.index("nft -c -f") < script.index("/etc/ceo/ceo-worker.nft")
    assert 'id -u)" -eq 0' in script  # refuses to run without root
    assert "set -euo pipefail" in script
    for check in ("worker can't read CEO's settings", "worker has no sudo", "worker can't reach that local service",
                  "worker can't reach the home network", "worker can't read or enter any home directory",
                  "no worker process survives CEO's cleanup", "can run commands as the worker",
                  "couldn't start a test process as the worker"):
        assert check in script
    # Negative network checks demand curl's "couldn't connect" (7), not just any failure.
    assert 'expect_exit 7 "worker can\'t reach that local service"' in script  # against a real listener
    assert '|| got=$?' in script  # failures are reported, not fatal under set -e
    assert 'can\'t read $SEED_ENV' in script
    assert "chown -R root:root /opt/ceo" in script  # neither user can change CEO's code
    assert "chmod 0640 /etc/ceo/env" in script


def test_seeded_chat_settings_include_signal():
    script = SCRIPT.read_text()
    seed = re.search(r"grep -E '\^\(([^)]*)\)=' \"\$SEED_ENV\"", script)
    assert seed and set(seed.group(1).split("|")) == {
        "CEO_CHANNEL", "CEO_MATRIX_[A-Z_]+", "CEO_SIGNAL_[A-Z_]+", "CEO_ENGINEER_CHAT"}


def test_home_directories_are_checked_for_entry_not_only_reading():
    script = SCRIPT.read_text()
    assert '{ test -r "$d" || test -x "$d"; }' in script
