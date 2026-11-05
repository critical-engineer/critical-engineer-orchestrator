import subprocess
from pathlib import Path

import pytest
from fake_http import FakeHttpApi

from ceo.approval import ApprovalError, GitHub, PlanRepo, check_plan
from ceo.gates import Engineer, decide_gate
from ceo.ledger import open_ledger

ENGINEER = Engineer(chat=frozenset({"@me:x"}), github=frozenset({"driessamyn"}))
REPO = "critical-engineer/plans"
CHANGE = "media-box"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class Repos:
    """A bare 'GitHub' origin, the engineer's working clone, and CEO's clone."""

    def __init__(self, tmp: Path):
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.ceo = tmp / "ceo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True, capture_output=True)
        for k, v in (("user.name", "Engineer"), ("user.email", "e@example.com"), ("commit.gpgsign", "false")):
            git(self.work, "config", k, v)
        git(self.work, "checkout", "-q", "-b", "main")
        self.commit({"README.md": "plans\n"}, "init")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.ceo)], check=True, capture_output=True)

    def commit(self, files: dict, message: str, branch: str = "main") -> str:
        if git(self.work, "symbolic-ref", "--short", "HEAD") != branch:
            git(self.work, "checkout", "-q", branch)
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git(self.work, "add", name)
        git(self.work, "commit", "-q", "-m", message)
        git(self.work, "push", "-q", "origin", branch)
        return git(self.work, "rev-parse", "HEAD")

    def merge_pr(self, branch: str, files: dict) -> tuple[str, str]:
        """A PR branch merged with a merge commit; returns (feature commit, merge commit)."""
        git(self.work, "checkout", "-q", "-b", branch, "main")
        feature = self.commit(files, f"plan on {branch}", branch=branch)
        git(self.work, "checkout", "-q", "main")
        git(self.work, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        git(self.work, "push", "-q", "origin", "main")
        return feature, git(self.work, "rev-parse", "HEAD")


PLAN_V1 = {f"openspec/changes/{CHANGE}/tasks.md": "- [ ] 1.1 Do it\n"}
PLAN_V2 = {f"openspec/changes/{CHANGE}/tasks.md": "- [ ] 1.1 Do it differently\n"}


@pytest.fixture
def repos(tmp_path):
    return Repos(tmp_path)


@pytest.fixture
def api():
    fake = FakeHttpApi()
    yield fake
    fake.close()


@pytest.fixture
def github(api):
    return GitHub(REPO, token="ghp_secret", api=api.url, timeout=5)


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    c.execute("INSERT INTO plans (repo, change) VALUES (?, ?)", (REPO, CHANGE))
    c.commit()
    yield c
    c.close()


def github_says(api, merge_commit, merger="driessamyn", number=7):
    api.routes = {
        ("GET", f"/repos/{REPO}/commits/"): (
            200, [{"number": number, "merged_at": "2026-11-05T10:00:00Z", "merge_commit_sha": merge_commit}],
        ),
        ("GET", f"/repos/{REPO}/pulls/{number}"): (200, {"number": number, "merged_by": {"login": merger}}),
    }


def check(conn, repos, github):
    return check_plan(conn, 1, PlanRepo(repos.ceo), github, ENGINEER)


def kinds(conn):
    return [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")]


def approved_sha(conn):
    return conn.execute("SELECT approved_sha FROM plans WHERE id = 1").fetchone()[0]


def test_plan_not_on_main(conn, repos, github):
    assert check(conn, repos, github).status == "not_on_main"
    assert approved_sha(conn) is None


def test_merge_by_the_engineer_approves(conn, repos, github, api):
    _, merge = repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, merge)
    result = check(conn, repos, github)
    assert (result.status, result.commit, result.merger) == ("approved", merge, "driessamyn")
    assert approved_sha(conn) == merge
    gate = conn.execute("SELECT kind, status, decided_route, decided_identity FROM gates").fetchone()
    assert tuple(gate) == ("plan_approval", "approved", "github", "driessamyn")
    assert "plan.approved" in kinds(conn)
    # The token went to GitHub as a bearer token.
    assert api.requests[0][2]["Authorization"] == "Bearer ghp_secret"


def test_squash_merge_approves(conn, repos, github, api):
    squash = repos.commit(PLAN_V1, "Plan v1 (#7)")
    github_says(api, squash)
    assert check(conn, repos, github).status == "approved"


def test_approval_is_stable_until_the_plan_changes(conn, repos, github, api):
    _, merge = repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, merge)
    check(conn, repos, github)
    repos.commit({"README.md": "unrelated change\n"}, "docs")  # main moves, plan doesn't
    calls = len(api.requests)
    assert check(conn, repos, github).status == "approved"
    assert len(api.requests) == calls  # no GitHub lookup needed


def test_plan_changed_after_approval_halts_until_reapproved(conn, repos, github, api):
    _, merge1 = repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, merge1)
    check(conn, repos, github)
    direct = repos.commit(PLAN_V2, "edit plan directly on main")  # no PR
    api.routes[("GET", f"/repos/{REPO}/commits/")] = (200, [])
    result = check(conn, repos, github)
    assert result.status == "pending"  # F3: approval withdrawn, nobody verified the new version
    assert approved_sha(conn) is None
    assert "plan.changed_after_approval" in kinds(conn) and "plan.approval_unverified" in kinds(conn)
    # The engineer can still approve the open gate by another route (chat or skill).
    decide_gate(conn, result.gate_id, "approved", route="chat", identity="@me:x", engineer=ENGINEER)
    result = check(conn, repos, github)
    assert (result.status, approved_sha(conn)) == ("approved", direct)


def test_merge_by_someone_else_leaves_the_gate_open(conn, repos, github, api):
    _, merge = repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, merge, merger="someone-else")
    result = check(conn, repos, github)
    assert (result.status, result.merger) == ("pending", "someone-else")
    assert approved_sha(conn) is None
    assert conn.execute("SELECT status FROM gates").fetchone()[0] == "open"


def test_bot_merge_does_not_approve(conn, repos, github, api):
    _, merge = repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, merge, merger="ceo-bot")
    assert check(conn, repos, github).status == "pending"


def test_pr_whose_merge_commit_is_not_on_main_is_ignored(conn, repos, github, api):
    repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, "f" * 40)  # GitHub points at a commit main doesn't contain
    assert check(conn, repos, github).status == "pending"


def test_pr_with_different_plan_contents_is_ignored(conn, repos, github, api):
    _, merge1 = repos.merge_pr("plan-v1", PLAN_V1)
    repos.commit(PLAN_V2, "edit plan directly on main")
    github_says(api, merge1)  # the PR did merge, but not the plan that's on main now
    assert check(conn, repos, github).status == "pending"


def test_ambiguous_prs_are_not_trusted(conn, repos, github, api):
    _, merge = repos.merge_pr("plan-v1", PLAN_V1)
    api.routes = {
        ("GET", f"/repos/{REPO}/commits/"): (200, [
            {"number": 7, "merged_at": "t", "merge_commit_sha": merge},
            {"number": 8, "merged_at": "t", "merge_commit_sha": merge},
        ]),
        ("GET", f"/repos/{REPO}/pulls/"): (200, {"merged_by": {"login": "driessamyn"}}),
    }
    assert check(conn, repos, github).status == "pending"


def test_denied_plan_gate(conn, repos, github, api):
    _, merge = repos.merge_pr("plan-v1", PLAN_V1)
    github_says(api, merge, merger="someone-else")
    result = check(conn, repos, github)
    decide_gate(conn, result.gate_id, "denied", route="chat", identity="@me:x", engineer=ENGINEER,
                reason="the design ignores F3")
    assert check(conn, repos, github).status == "denied"
    assert conn.execute("SELECT count(*) FROM gates").fetchone()[0] == 1  # same plan, same gate


def test_github_errors_are_approval_errors(conn, repos, github, api):
    repos.merge_pr("plan-v1", PLAN_V1)
    api.routes = {("GET", f"/repos/{REPO}/commits/"): (403, {"message": "rate limited"})}
    with pytest.raises(ApprovalError, match="HTTP 403"):
        check(conn, repos, github)


@pytest.mark.parametrize("change", ["../etc", "Media Box", "-x", ""])
def test_change_names_are_validated(conn, repos, github, change):
    conn.execute("UPDATE plans SET change = ? WHERE id = 1", (change,))
    conn.commit()
    with pytest.raises(ApprovalError, match="change name"):
        check(conn, repos, github)


def test_repo_names_are_validated():
    with pytest.raises(ApprovalError):
        GitHub("not a repo")


def test_merge_commit_must_be_on_main(conn, repos, github, api):
    # An unmerged branch has the same plan contents as main; GitHub claims that
    # branch commit was a merge. It exists and matches, but main doesn't contain it.
    git(repos.work, "checkout", "-q", "-b", "side", "main")
    side = repos.commit(PLAN_V1, "same plan on a side branch", branch="side")
    repos.commit(PLAN_V1, "plan pushed straight to main", branch="main")
    git(repos.ceo, "fetch", "-q", "origin", "side")
    github_says(api, side)
    assert check(conn, repos, github).status == "pending"
