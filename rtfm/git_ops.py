"""Git primitives: thin, classified wrappers over the git CLI.

Every failure surfaces as RuntimeError with a cause-bearing message; callers
distinguish "not a repo" (None/"" answers) from "git is broken" (raises).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import git_timeout


def run(args: list[str], cwd: Path, timeout: int | None = None,
        allow_exit_codes: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """Run a git command. Raises RuntimeError on non-zero exit, timeout, or
    subprocess failure (missing/untraversable cwd, missing git binary, permissions).
    `allow_exit_codes` are non-zero exits that are legitimate answers, not failures
    (git merge-base --is-ancestor exits 1 for a genuine 'not an ancestor')."""
    t = timeout if timeout is not None else git_timeout()
    if not cwd.is_dir():
        # subprocess.run raises FileNotFoundError for a missing cwd exactly as for a
        # missing git binary — distinguish, or a deleted clone would be misreported
        # as "git executable not found".
        raise RuntimeError(f"path {cwd} does not exist")
    try:
        cp = subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
            timeout=t if t > 0 else None,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} timed out after {t}s") from None
    except FileNotFoundError:
        raise RuntimeError(
            "git executable not found — install git or remove the git_repo source.") from None
    except OSError as e:
        # PermissionError on an untraversable cwd (chmod-000 dir passes is_dir but
        # fails in chdir), EACCES on a non-executable git in PATH, EAGAIN — all
        # OSErrors the RuntimeError-only guards would otherwise leak raw.
        raise RuntimeError(f"git failed: {e}") from None
    if cp.returncode != 0 and cp.returncode not in allow_exit_codes:
        raise RuntimeError(
            f"git {' '.join(args)} exited {cp.returncode}: {(cp.stderr or '').strip()[:200]}")
    return cp


def repo_root(path: Path) -> Path | None:
    """Return the git repo root, or None if `path` is not in a git repo.

    A missing git binary or any other git failure (corruption, permissions,
    ownership guard, timeout) raises RuntimeError — collapsing them to None would
    misreport a machine without git as NOT_GIT_REPO and a corrupt repo as "not a
    repo" with the wrong recovery advice. Only git's own "not a git repository"
    verdict returns None.
    """
    try:
        cp = run(["rev-parse", "--show-toplevel"], cwd=path, timeout=10)
        return Path(cp.stdout.strip())
    except RuntimeError as e:
        if re.search(r"not a git repository", str(e), re.IGNORECASE):
            return None
        raise


def remote_url(path: Path) -> str:
    """Return the origin remote URL, or '' if the repo has no origin remote.

    Any other git failure (missing binary, corruption, timeout) raises — collapsing
    it to '' would misreport a failing clone as 'no origin' and silently skip the
    remote-mismatch check that protects the index.
    """
    try:
        cp = run(["remote", "get-url", "origin"], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError as e:
        if "No such remote 'origin'" in str(e):
            return ""
        raise


def is_clean(path: Path) -> tuple[bool, list[str]]:
    """(is_clean, list_of_dirty_files). A clean tree has no modified or untracked files."""
    cp = run(["status", "--porcelain"], cwd=path, timeout=10)
    if not cp.stdout.strip():
        return True, []
    # Parse dirty file paths: `git status --porcelain` gives "XY filename". splitlines()
    # + slice, not strip+split: an unstaged edit is " M file" — strip() would eat the
    # leading status space and shift the slice one column left, and split() breaks on
    # filenames containing spaces.
    dirty = [line[3:].strip() for line in cp.stdout.splitlines() if len(line) >= 4]
    return False, dirty


def current_commit(path: Path) -> str:
    """Return the SHA of HEAD."""
    cp = run(["rev-parse", "HEAD"], cwd=path, timeout=10)
    return cp.stdout.strip()


def commit_date(path: Path, ref: str) -> str:
    """Return ISO 8601 commit date for `ref`."""
    cp = run(["log", "-1", "--format=%cI", ref], cwd=path, timeout=10)
    return cp.stdout.strip()


def resolve_ref(path: Path, ref: str) -> str:
    """Resolve a ref to a commit SHA. For branches, tries origin/<ref> first.
    For tags and SHAs, resolves directly. Raises RuntimeError if ref doesn't exist."""
    # Branches: origin/<ref> first — after a fetch, origin/<ref> is the remote's current
    # commit while the local branch may be behind, so local-first would hide remote updates.
    try:
        cp = run(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError as e:
        if "git executable not found" in str(e):
            raise  # missing git is not 'ref not found'
    # Fall back to local (works for SHAs, tags, local-only branches, HEAD)
    try:
        cp = run(["rev-parse", "--verify", ref], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError:
        raise RuntimeError(
            f"ref '{ref}' not found as origin/{ref} or locally in {path}. "
            f"Recover: verify the ref name, or run 'git fetch' in the repo.") from None


def fetch(path: Path, timeout: int | None = None) -> None:
    """git fetch origin. Timeout-guarded; raises RuntimeError on failure."""
    t = timeout if timeout is not None else git_timeout()
    run(["fetch", "origin"], cwd=path, timeout=t)


def clone(url: str, ref: str | None, dest: Path, timeout: int | None = None) -> None:
    """Clone a repo. Full clone (no --depth). With a ref, clones and checks out that
    branch/tag; with ref=None or a pinned SHA, clones the remote's default branch
    (git rejects `--branch HEAD`, and `git clone --branch <sha>` fails — the pinned
    SHA is checked out afterwards by checkout's detached path). Raises RuntimeError."""
    t = timeout if timeout is not None else git_timeout()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"could not create {dest.parent}: {e}") from None
    args = ["clone", url, str(dest)]
    if ref:
        args[1:1] = ["--branch", ref]
    try:
        run(args, cwd=dest.parent, timeout=t)
    except RuntimeError as e:
        # A hex-shaped ref might be a branch named like a SHA (git treats it as a
        # branch name — the --branch form above already succeeded for those) or a
        # true pinned SHA, which git rejects in --branch (its error: "Remote branch
        # <sha> not found"). Only THAT failure retries — a bad URL or network error
        # must raise immediately, not pay for a second full clone.
        if not (ref and is_sha(ref)) or "Remote branch" not in str(e):
            raise
        args[1:3] = []                       # drop --branch <ref>, keep url + dest
        run(args, cwd=dest.parent, timeout=t)


def checkout(path: Path, ref: str) -> None:
    """Move the working tree to `ref`. For branches: reset to match origin/<ref>.
    For tags/SHAs: direct checkout, with one retry that fetches the tag by explicit
    refspec (a plain fetch cannot materialize a tag whose commit is unreachable from
    any branch). Raises RuntimeError on failure.

    Managed clones only — linked clones are read-only (ADR 0013). The caller has
    already fetched, so origin/<ref> is current; the branch path must not fetch
    again (that doubled network traffic on every refresh).
    """
    # Determine if `ref` is a branch by checking if origin/<ref> exists
    branch = False
    try:
        run(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        branch = True
    except RuntimeError as e:
        if "git executable not found" in str(e):
            raise  # missing git is not 'not a branch'

    if branch:
        # Reset to match the remote the caller just fetched
        run(["checkout", "-B", ref, f"origin/{ref}"], cwd=path,
            timeout=git_timeout())
    else:
        # Tag or SHA: direct checkout. A plain `git fetch origin` does not bring a
        # tag whose commit is unreachable from a fetched branch, so the retry must
        # fetch the tag by explicit refspec — and only when the failure was the
        # ref missing locally (a dirty tree would fail identically after the fetch).
        try:
            run(["checkout", ref], cwd=path, timeout=git_timeout())
        except RuntimeError as e:
            if "did not match any file(s) known to git" not in str(e):
                raise
            try:
                run(["fetch", "origin", f"refs/tags/{ref}:refs/tags/{ref}"],
                    cwd=path, timeout=git_timeout())
            except RuntimeError:
                raise e from None           # the ref really doesn't exist — original error
            run(["checkout", ref], cwd=path, timeout=git_timeout())


def is_sha(ref: str) -> bool:
    """True for a hex-shaped ref (full or short SHA-1 form). Hex shape alone does not
    make a pin — a branch named 'deadbeef' is a branch — that decision is
    is_sha_pin's. A pinned SHA puts the checkout in git's detached-HEAD state:
    staleness undefined (ADR 0013)."""
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", ref))


def is_sha_pin(path: Path, ref: str) -> bool:
    """True if `ref` is a pinned SHA (hex-shaped AND not a branch). A branch named
    'deadbeef' is a normal branch to git, not a pin — only a ref that does not
    resolve as any branch is treated as detached."""
    return is_sha(ref) and not is_branch(path, ref)


def is_branch(path: Path, ref: str) -> bool:
    """True if `ref` resolves as a branch (origin-tracking or local)."""
    try:
        run(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        return True
    except RuntimeError as e:
        if "git executable not found" in str(e):
            raise
    try:
        run(["rev-parse", "--verify", f"refs/heads/{ref}"], cwd=path, timeout=10)
        return True
    except RuntimeError:
        return False


def is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is an ancestor of `descendant` (both must resolve locally).

    git exits 1 for the legitimate 'not an ancestor' answer and 128 for errors; only
    the exit-1 case returns False, so a git failure raises and the caller degrades
    to 'unknown' instead of reporting a wrong 'diverged'."""
    cp = run(["merge-base", "--is-ancestor", ancestor, descendant],
             cwd=path, timeout=10, allow_exit_codes=(1,))
    return cp.returncode == 0


def normalize_remote(url: str) -> str:
    """Canonical form for remote comparison: scheme/credentials stripped, explicit
    ports dropped (ssh://host:22/..., https://host:443/...), scp-style 'host:path'
    made slash-form, trailing '.git' and '/' dropped, lowercased. Two URLs for the
    same repo (https vs ssh, ported vs not, trailing slash or not) compare equal.
    The theoretical collision — a real repo at 'host:port/repo' vs one at
    'host/port/repo' — is not expected for real repo URLs."""
    u = url.strip()
    if "://" in u:
        u = u.split("://", 1)[1]
    if "@" in u:
        u = u.split("@", 1)[1]
    u = re.sub(r":\d+(?=/|$)", "", u)        # explicit port, not the scp path colon
    u = u.replace(":", "/", 1)                # scp form git@host:org/repo.git
    if u.endswith(".git"):
        u = u[:-4]
    u = u.rstrip("/")
    return u.lower()
