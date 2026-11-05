"""GitHub approval gate (T5): approval is the engineer merging the plan to main.

A plan is an OpenSpec change, ``openspec/changes/<change>``, in the plan repo.
CEO keeps its own clone, fetches ``main`` and fingerprints the change directory
with git's tree hash, which changes exactly when anything in the directory does.

Who approved it matters (C10): CEO finds the merged PR that brought the current
plan onto ``main`` and asks GitHub who merged it. If that's one of the
engineer's GitHub logins, the ``plan_approval`` gate is decided as them, by
route ``github``. Anything else (a merge by someone else, a direct push, an
ambiguous history) leaves the gate open for the engineer to decide by chat or
the skill.

If the plan on ``main`` changes after approval, the approval is withdrawn and
execution halts until the new version is approved (F3); syncing the new
version then blocks tasks whose definition changed after they ran (T4).

Configuration (environment): CEO_GITHUB_TOKEN (optional for a public repo;
read-only access is enough, and it is never printed).
"""

import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ceo.gates import Engineer, decide_gate, digest, open_gate
from ceo.ledger import record_event
from ceo.notify import ChannelError, open_url

_CHANGE = re.compile(r"[a-z0-9][a-z0-9-]*")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")


class ApprovalError(Exception):
    pass


@dataclass
class PlanRepo:
    """CEO's own clone of the plan repo."""

    path: Path
    branch: str = "main"

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args], capture_output=True, text=True, timeout=120
        )
        if check and result.returncode != 0:
            raise ApprovalError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result

    def fetch(self) -> None:
        self._git("fetch", "--quiet", "origin", self.branch)

    def head(self) -> str:
        return self._git("rev-parse", f"origin/{self.branch}").stdout.strip()

    def tree(self, commit: str, change: str) -> str | None:
        """The change directory's tree hash at ``commit``, or None if it isn't there."""
        result = self._git("rev-parse", "--verify", "--quiet", f"{commit}:openspec/changes/{change}", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def last_change(self, commit: str, change: str) -> str:
        return self._git("log", "-1", "--format=%H", commit, "--", f"openspec/changes/{change}").stdout.strip()

    def is_ancestor(self, ancestor: str, commit: str) -> bool:
        return self._git("merge-base", "--is-ancestor", ancestor, commit, check=False).returncode == 0


@dataclass
class GitHub:
    """Read-only lookups of who merged what."""

    repo: str  # "owner/name"
    token: str | None = None
    api: str = "https://api.github.com"
    timeout: float = 30

    def __post_init__(self):
        if not _REPO.fullmatch(self.repo):
            raise ApprovalError(f"not a GitHub repo name: {self.repo!r}")

    def _get(self, path: str):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "critical-engineer-orchestrator",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.api.rstrip("/") + path, headers=headers)
        try:
            return json.loads(open_url(request, self.timeout))
        except urllib.error.HTTPError as exc:
            raise ApprovalError(f"GitHub GET {path}: HTTP {exc.code}") from exc
        except ChannelError as exc:
            raise ApprovalError(f"GitHub GET {path}: {exc}") from exc
        except ValueError as exc:
            raise ApprovalError(f"GitHub GET {path}: response is not JSON") from exc

    def merged_pulls(self, commit: str) -> list[dict]:
        """Merged PRs that contain ``commit``: [{number, merge_commit_sha}]."""
        pulls = self._get(f"/repos/{self.repo}/commits/{commit}/pulls")
        if not isinstance(pulls, list):
            raise ApprovalError("GitHub: unexpected response listing pull requests")
        return [
            {"number": p["number"], "merge_commit_sha": p.get("merge_commit_sha")}
            for p in pulls
            if isinstance(p, dict) and p.get("merged_at") and isinstance(p.get("number"), int)
        ]

    def merged_by(self, number: int) -> str | None:
        pr = self._get(f"/repos/{self.repo}/pulls/{number}")
        merger = pr.get("merged_by") if isinstance(pr, dict) else None
        return merger.get("login") if isinstance(merger, dict) else None


@dataclass(frozen=True)
class Approval:
    status: str  # "approved", "pending" (gate open), "denied", "not_on_main"
    commit: str | None = None
    gate_id: int | None = None
    merger: str | None = None


def _merger_of_current_plan(repo: PlanRepo, github: GitHub, head: str, change: str, tree: str) -> tuple[str | None, str]:
    """(login, why) for the one merged PR that put the current plan on main."""
    changed_in = repo.last_change(head, change)
    candidates = [
        p
        for p in github.merged_pulls(changed_in)
        if isinstance(p["merge_commit_sha"], str)
        and _SHA.fullmatch(p["merge_commit_sha"])
        and repo.is_ancestor(p["merge_commit_sha"], head)
        and repo.tree(p["merge_commit_sha"], change) == tree
    ]
    if len(candidates) != 1:
        return None, f"{len(candidates)} merged PRs match the plan on main"
    login = github.merged_by(candidates[0]["number"])
    return login, f"PR #{candidates[0]['number']}"


def check_plan(
    conn: sqlite3.Connection,
    plan_id: int,
    repo: PlanRepo,
    github: GitHub,
    engineer: Engineer,
) -> Approval:
    """Is the plan on main approved by the engineer? Records the outcome in the ledger."""
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        raise ApprovalError(f"no plan {plan_id}")
    change = plan["change"]
    if not _CHANGE.fullmatch(change):
        raise ApprovalError(f"not a valid change name: {change!r}")

    repo.fetch()
    head = repo.head()
    tree = repo.tree(head, change)
    if tree is None:
        return Approval("not_on_main")

    if plan["approved_sha"]:
        if repo.tree(plan["approved_sha"], change) == tree:
            return Approval("approved", plan["approved_sha"])
        # F3: the approved plan changed on main. Withdraw approval and halt.
        with conn:
            conn.execute("UPDATE plans SET approved_sha = NULL WHERE id = ?", (plan_id,))
        record_event(
            conn,
            "plan.changed_after_approval",
            plan_id=plan_id,
            detail={"approved_sha": plan["approved_sha"], "head": head},
        )

    payload = {"repo": plan["repo"], "change": change, "tree": tree}
    gate = conn.execute(
        "SELECT * FROM gates WHERE plan_id = ? AND kind = 'plan_approval' AND payload_sha256 = ?"
        " ORDER BY id DESC LIMIT 1",
        (plan_id, digest(payload)),
    ).fetchone()
    gate_id = gate["id"] if gate else open_gate(conn, plan_id, "plan_approval", payload)
    status = gate["status"] if gate else "open"

    merger = None
    if status == "open":
        merger, why = _merger_of_current_plan(repo, github, head, change, tree)
        if merger and engineer.allows("github", merger):
            decide_gate(conn, gate_id, "approved", route="github", identity=merger, engineer=engineer)
            status = "approved"
        else:
            record_event(
                conn,
                "plan.approval_unverified",
                plan_id=plan_id,
                detail={"gate": gate_id, "merger": merger, "why": why},
            )

    if status == "denied":
        return Approval("denied", head, gate_id)
    if status != "approved":
        return Approval("pending", head, gate_id, merger)
    with conn:
        conn.execute("UPDATE plans SET approved_sha = ? WHERE id = ?", (head, plan_id))
    record_event(conn, "plan.approved", plan_id=plan_id, detail={"sha": head, "tree": tree, "gate": gate_id})
    return Approval("approved", head, gate_id, merger)
