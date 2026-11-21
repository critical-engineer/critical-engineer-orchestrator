"""Per-task workspaces, built so a worker can't reach CEO's git or files (T9, C4).

The worker gets a *plain export* of the approved commit: files only, no
``.git``. If it could edit git metadata it could point CEO at hooks that run as
the ``ceo`` user when CEO commits. When the worker is done, CEO copies the
files back into its own private worktree and commits there, with hooks
disabled, so the result is one commit per task (D8). The copy:

- skips any ``.git`` directory or file the worker created;
- keeps symlinks as symlinks, never following them (a link to /etc/... can't
  pull a file into the commit);
- skips anything that isn't a regular file, directory or symlink.
"""

import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

_GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false"]


class WorkspaceError(Exception):
    pass


def _git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *_GIT_SAFE, "-C", str(repo), *args], capture_output=True, input=input, timeout=300
    )
    if result.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def export(repo: Path, commit: str, dest: Path, *, group_writable: bool = True) -> Path:
    """Write the files of ``commit`` into a new directory ``dest``, without ``.git``."""
    dest.mkdir(parents=True)
    archive = _git(repo, "archive", "--format=tar", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    if group_writable:  # the worker user shares a group with CEO (set up on the box)
        for path in [dest, *dest.rglob("*")]:
            if not path.is_symlink():
                mode = path.stat().st_mode
                path.chmod(mode | stat.S_IRGRP | stat.S_IWGRP | (stat.S_IXGRP if path.is_dir() else 0))
    return dest


def _copy_back(src: Path, dest: Path) -> None:
    """Mirror the worker's files into ``dest`` (CEO's worktree), safely."""
    for entry in dest.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for root, dirs, files in os.walk(src, followlinks=False):
        here = Path(root)
        target_dir = dest / here.relative_to(src)
        target_dir.mkdir(exist_ok=True)
        descend = []
        for name in dirs:  # os.walk lists symlinks to directories here too
            if name == ".git":
                continue
            if (here / name).is_symlink():
                os.symlink(os.readlink(here / name), target_dir / name)
            else:
                descend.append(name)
        dirs[:] = descend
        for name in files:
            if name == ".git":
                continue
            path, target = here / name, target_dir / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                os.symlink(os.readlink(path), target)
            elif stat.S_ISREG(mode):
                shutil.copyfile(path, target, follow_symlinks=False)
                target.chmod(0o755 if mode & stat.S_IXUSR else 0o644)
            # fifos, sockets and devices are dropped


def commit_result(repo: Path, base: str, worker_dir: Path, *, branch: str, message: str) -> str | None:
    """Commit the worker's files on top of ``base`` as ``branch``. None if nothing changed."""
    with tempfile.TemporaryDirectory(prefix="ceo-commit-") as tmp:
        tree = Path(tmp) / "tree"
        _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), base)
        try:
            _copy_back(worker_dir, tree)
            _git(tree, "add", "--all")
            if not _git(tree, "status", "--porcelain"):
                return None
            _git(
                tree,
                "-c", "user.name=CEO",
                "-c", "user.email=ceo@localhost",
                "commit", "--quiet", "--no-verify", "-m", message,
            )
            sha = _git(tree, "rev-parse", "HEAD").decode().strip()
            _git(repo, "branch", "--force", branch, sha)
            return sha
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))


def _git_answer(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git query whose exit code 1 means "no". Any other failure raises:
    a broken repo must never be mistaken for a negative answer, since callers
    reset branches on "no"."""
    result = subprocess.run(["git", *_GIT_SAFE, "-C", str(repo), *args], capture_output=True, timeout=60)
    if result.returncode not in (0, 1):
        raise WorkspaceError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def branch_tip(repo: Path, branch: str) -> str | None:
    result = _git_answer(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.stdout.decode().strip() if result.returncode == 0 else None


def is_ancestor(repo: Path, ancestor: str, commit: str) -> bool:
    return _git_answer(repo, "merge-base", "--is-ancestor", ancestor, commit).returncode == 0


def set_branch(repo: Path, branch: str, commit: str) -> None:
    _git(repo, "branch", "--force", branch, commit)


def fast_forward(repo: Path, branch: str, commit: str) -> bool:
    """Move ``branch`` to ``commit`` only if that's a fast-forward. Returns whether it moved.

    Integration never merges or cherry-picks: a commit that isn't a descendant of
    the branch tip was built on older work, and its check didn't run against
    what the branch holds now.
    """
    current = branch_tip(repo, branch)
    if current is not None and not is_ancestor(repo, current, commit):
        return False
    set_branch(repo, branch, commit)
    return True
