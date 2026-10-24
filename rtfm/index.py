"""The content-addressed index and its freshness verdicts (ADRs 0010, 0013, 0015).

The index DB is a cache: a schema mismatch drops it, and a reindex repopulates it.
Freshness asks whether the index still matches a source; the store module owns
both the answer and the ingest that acts on it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from . import git_ops, staging
from .config import SCHEMA_VERSION, index_db_path, managed_repo_path
from .manifest import Source
from .staging import CacheStats, Indexed, StepResult


def linked_git_status(repo_path: Path, src: Source, indexed: str) -> str:
    """git_status for a linked clone, in git's own terms — read-only: compares the
    indexed commit against the LOCAL origin/<ref> (origin first, local fallback for
    tags/SHAs/local-only branches — the clone's own knowledge; rtfm never fetches a
    linked clone, ADR 0013). A pinned SHA puts the checkout in git's detached-HEAD
    state — staleness undefined. An unresolvable ref means 'unknown' — check the ref
    spelling in the manifest."""
    try:
        ref = src.ref or default_branch(repo_path)
        if git_ops.is_sha_pin(repo_path, ref):
            return "detached"
        origin_sha = git_ops.resolve_ref(repo_path, ref)   # origin/<ref> first, local fallback
    except RuntimeError:
        return "unknown"
    if indexed == origin_sha:
        return "up to date"
    if git_ops.is_ancestor(repo_path, origin_sha, indexed):
        return "ahead"
    if git_ops.is_ancestor(repo_path, indexed, origin_sha):
        return "behind"
    return "diverged"


def managed_git_status(repo_path: Path, src: Source, indexed: str) -> str:
    """git_status for a managed clone: fetch (rtfm owns the clone), then compare the
    indexed commit to origin/<ref> (origin first, local fallback). The reset keeps
    HEAD at origin, so 'ahead' and 'diverged' are unreachable here. A pinned SHA
    needs no fetch — the pin never moves — and reports git's detached-HEAD state."""
    try:
        ref = src.ref or default_branch(repo_path)
        if git_ops.is_sha_pin(repo_path, ref):
            return "detached"
        git_ops.fetch(repo_path)
        current = git_ops.resolve_ref(repo_path, ref)
    except RuntimeError:
        return "unknown"
    if indexed == current:
        return "up to date"
    return "behind"

def get_index_db() -> sqlite3.Connection:
    p = index_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        migrate_schema(conn)
    return conn


def migrate_schema(conn: sqlite3.Connection) -> None:
    """The index DB is a cache. On any version mismatch, drop all prior tables and rebuild
    the empty content-addressed schema; the next reindex repopulates it."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS doc_meta;
        DROP TABLE IF EXISTS doc_fts;
        DROP TABLE IF EXISTS contents;
        DROP TABLE IF EXISTS locations;
        DROP TABLE IF EXISTS content_fts;
        DROP TABLE IF EXISTS source_meta;
        CREATE VIRTUAL TABLE doc_fts USING fts5(sha256 UNINDEXED, title, headings);
        CREATE TABLE contents (
            sha256       TEXT PRIMARY KEY,
            locator_kind TEXT NOT NULL,
            n_chunks     INTEGER NOT NULL,
            extracted_ok INTEGER NOT NULL,
            error        TEXT
        );
        CREATE TABLE locations (
            source  TEXT NOT NULL,
            relpath TEXT NOT NULL,
            sha256  TEXT NOT NULL,
            mtime   REAL NOT NULL,
            PRIMARY KEY (source, relpath)
        );
        CREATE INDEX idx_locations_sha ON locations(sha256);
        CREATE VIRTUAL TABLE content_fts USING fts5(
            sha256 UNINDEXED, locator_kind UNINDEXED, locator_value UNINDEXED, text
        );
        CREATE TABLE source_meta (
            source          TEXT PRIMARY KEY,
            git_commit      TEXT NOT NULL,
            git_commit_date TEXT NOT NULL,
            config_scope    TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


AUTO_REINDEX_MAX_FILES = 10          # inline new/changed-file budget on query (0 = none)


def auto_reindex_max() -> int:
    """Max new/changed files a source may have for `search` to reindex it inline. Bounds query
    latency (PDF extraction is the cost); larger deltas fall back to a 'run reindex' warning.
    RTFM_AUTO_REINDEX_MAX overrides; 0 disables inline reindexing of new/changed files (files
    that vanished on disk are still purged inline — that costs nothing)."""
    env = os.environ.get("RTFM_AUTO_REINDEX_MAX")
    if env and env.isdigit():
        return int(env)
    return AUTO_REINDEX_MAX_FILES


def git_missing_error() -> str:
    """Classified error for a missing git binary — without the check, a machine
    without git would be misreported as NOT_GIT_REPO."""
    return ("ERROR:GIT_MISSING: git executable not found. "
            "Recover: install git, or remove the git_repo source.")


def git_error_class(e: RuntimeError) -> str:
    """Classified error for a git failure that isn't one of the known classes:
    GIT_MISSING for an absent binary, GIT_FAILED for everything else (corruption,
    permissions, ownership guard, timeout) — never misreported as 'not a repo'."""
    if "git executable not found" in str(e):
        return git_missing_error()
    return (f"ERROR:GIT_FAILED: {e}. "
            "Recover: fix the repo's git state (permissions, ownership, corruption) "
            "and try again.")


def dirty_error(repo_path: Path) -> str | None:
    """Classified DIRTY_TREE error if the working tree has changes, else None."""
    clean, dirty_files = git_ops.is_clean(repo_path)
    if not clean:
        return (
            f"ERROR:DIRTY_TREE: {len(dirty_files)} file(s) modified or untracked — "
            f"{', '.join(dirty_files[:10])}. "
            f"Recover: commit, stash, or clean the working tree, then reindex.")
    return None


def ensure_git_repo_ready(src: Source) -> tuple[Path, str | None]:
    """Ensure a git_repo source has a clone at the right commit. Returns (repo_path, error).
    On success, error is None. On failure, error is a classified message.

    Linked mode is read-only (ADR 0013): rtfm never fetches or checks out the user's
    clone — it verifies the repo, the remote URL, and a clean tree, then indexes the
    tree as-is. Managed clones are rtfm's own: clone/verify, fetch, dirty-check, checkout.
    """
    if src.path is not None:
        repo_path = src.path
        # Linked mode: verify repo + remote + clean tree only (no fetch, no checkout).
        if not repo_path.exists():
            return repo_path, (
                f"ERROR:PATH_MISSING: {repo_path} does not exist. "
                f"Recover: clone the repo there, or remove 'path' from the manifest "
                f"to let rtfm manage the clone.")
        try:
            root = git_ops.repo_root(repo_path)
        except RuntimeError as e:
            return repo_path, git_error_class(e)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} is not a git repository. "
                f"Recover: clone the repo or remove 'path' from the manifest.")
        if src.url:
            try:
                remote = git_ops.remote_url(root)
            except RuntimeError as e:
                return repo_path, git_error_class(e)
            if remote and git_ops.normalize_remote(remote) != git_ops.normalize_remote(src.url):
                return repo_path, (
                    f"ERROR:REMOTE_MISMATCH: origin is '{remote}', manifest declares "
                    f"'{src.url}'. Recover: fix the manifest or update the clone's origin.")
        try:
            error = dirty_error(repo_path)
        except RuntimeError as e:
            return repo_path, git_error_class(e)
        if error:
            return repo_path, error
        return repo_path, None

    # Managed mode: clone if needed
    repo_path = managed_repo_path(src.name)
    if not (repo_path / ".git").exists():
        try:
            git_ops.clone(src.url, src.ref, repo_path)   # ref None → default branch
        except RuntimeError as e:
            if "git executable not found" in str(e):
                return repo_path, git_missing_error()
            return repo_path, (
                f"ERROR:CLONE_FAILED: {e}. "
                f"Recover: verify the URL and network, or provide a 'path' to an "
                f"existing clone.")
    else:
        # Verify existing managed clone: repo first (a fake .git dir is NOT_GIT_REPO,
        # not a remote-URL failure), then the remote URL.
        try:
            root = git_ops.repo_root(repo_path)
        except RuntimeError as e:
            return repo_path, git_error_class(e)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} exists but is not a git repository. "
                f"Recover: remove {repo_path} and reindex.")
        try:
            remote = git_ops.remote_url(repo_path)
        except RuntimeError as e:
            return repo_path, git_error_class(e)
        if (remote and src.url
                and git_ops.normalize_remote(remote) != git_ops.normalize_remote(src.url)):
            return repo_path, (
                f"ERROR:REMOTE_MISMATCH: managed clone at {repo_path} has origin "
                f"'{remote}', manifest declares '{src.url}'. "
                f"Recover: remove {repo_path} and reindex, or fix the manifest.")

    # Ref to use
    try:
        ref = src.ref or default_branch(repo_path)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:NO_REMOTE: can't determine default branch: {e}. "
            f"Recover: set 'ref' in the manifest or add an 'origin' remote to {repo_path}.")

    # Fetch, dirty-check BEFORE checkout (a dirty tree would abort the reset with a
    # confusing checkout error — the classified DIRTY_TREE advice is the right one),
    # then checkout.
    try:
        git_ops.fetch(repo_path)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:FETCH_FAILED: {e}. "
            f"Recover: check network connectivity and try again.")

    try:
        error = dirty_error(repo_path)
    except RuntimeError as e:
        return repo_path, git_error_class(e)
    if error:
        return repo_path, error

    try:
        git_ops.checkout(repo_path, ref)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:CHECKOUT_FAILED: ref '{ref}' — {e}. "
            f"Recover: verify the ref exists on the remote and try again.")

    return repo_path, None


def ingest(conn: sqlite3.Connection, src: Source, root: Path, scan: staging.Scan) -> Indexed:
    """Steps 3A and 3B over an already-scanned source, and sync the index. Takes the `Scan`
    rather than making one, so a caller that already walked to decide freshness does not walk
    again (ADR 0015).

    `locations` is reconciled against what step 2 WANTED, not what step 3A read. A position
    that failed step 1 or 3A is present in the tree and unreadable — not absent. Computing
    `vanished` from what succeeded would delete its row and GC its content, losing indexed
    content because a file's permissions changed. Only step 2's output answers "what is in
    this source"."""
    source_name = src.name
    existing = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT relpath, sha256, mtime FROM locations WHERE source=?", (source_name,))}

    read = staging.read_bytes_for(scan.wanted, root, existing)

    # A position that failed 3A is still in `wanted` (read_bytes_for reports it as a problem,
    # not by omission), so it keeps its row. A de-selected file is present on disk and must
    # still be purged: presence is step 2's answer, not the filesystem's.
    vanished = set(existing) - set(staging.still_present(
        {rel: mtime for rel, (_sha, mtime) in existing.items()}, scan, root))
    for rel in vanished:
        conn.execute("DELETE FROM locations WHERE source=? AND relpath=?", (source_name, rel))
    for h in read.kept:
        conn.execute(
            "INSERT INTO locations(source, relpath, sha256, mtime) VALUES(?,?,?,?) "
            "ON CONFLICT(source, relpath) DO UPDATE SET "
            "sha256=excluded.sha256, mtime=excluded.mtime",
            (source_name, staging.rel(h.path, root), h.sha, h.mtime))
    conn.commit()

    shas_here = {h.sha for h in read.kept}
    already = {r[0] for r in conn.execute("SELECT sha256 FROM contents")}
    need = shas_here - already
    handled = staging.handle(read.kept, need, root)

    for ex in handled.kept:
        kind = ex.rows[0][0] if ex.rows else "line"
        conn.execute("DELETE FROM content_fts WHERE sha256=?", (ex.sha,))
        conn.executemany(
            "INSERT INTO content_fts(sha256, locator_kind, locator_value, text) VALUES(?,?,?,?)",
            [(ex.sha, k, v, t) for (k, v, t) in ex.rows])
        conn.execute("DELETE FROM doc_fts WHERE sha256=?", (ex.sha,))
        conn.execute("INSERT INTO doc_fts(sha256, title, headings) VALUES(?,?,?)",
                     (ex.sha, ex.title, ex.headings))
        conn.execute(
            "INSERT INTO contents(sha256, locator_kind, n_chunks, extracted_ok, error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET "
            "locator_kind=excluded.locator_kind, n_chunks=excluded.n_chunks, "
            "extracted_ok=excluded.extracted_ok, error=excluded.error",
            (ex.sha, kind, len(ex.rows), 0 if ex.error else 1, ex.error))
    conn.commit()

    for tbl in ("content_fts", "doc_fts", "contents"):       # GC contents with no live path
        conn.execute(
            f"DELETE FROM {tbl} WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM locations)")
    conn.commit()

    errors = sum(1 for ex in handled.kept if ex.error)
    return Indexed(
        reachable=scan.reachable, wanted=scan.wanted, read=read, handled=handled,
        cache=CacheStats(unique_contents=len(shas_here),
                         newly_extracted=len(handled.kept) - errors,
                         extraction_skips=len(scan.wanted) - len(need),
                         purged=len(vanished)))


def as_summary(result: Indexed) -> dict:
    """Today's counter dict, as a projection of `Indexed`. The JSON wire format does not move."""
    errors = sum(1 for ex in result.handled.kept if ex.error)
    return {"files_seen": len(result.wanted),
            "unique_contents": result.cache.unique_contents,
            "newly_extracted": result.cache.newly_extracted,
            "extraction_skips": result.cache.extraction_skips,
            "purged": result.cache.purged,
            "errors": errors}


EMPTY_INDEXED = Indexed(reachable=StepResult([], []), wanted=[],
                         read=StepResult([], []), handled=StepResult([], []),
                         cache=CacheStats(0, 0, 0, 0))


def reindex_git_repo(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild a git_repo source."""
    repo_path, error = ensure_git_repo_ready(src)
    if error:
        return {"source": src.name, "error": error}

    # Record commit before indexing
    try:
        commit = git_ops.current_commit(repo_path)
        commit_date = git_ops.commit_date(repo_path, "HEAD")
    except RuntimeError as e:
        if re.search(r"ambiguous argument 'HEAD'|Needed a single revision", str(e)):
            return {"source": src.name,
                    "error": (f"ERROR:EMPTY_REPO: {repo_path} has no commits yet. "
                              f"Recover: commit something first, or point 'ref' at an "
                              f"existing commit.")}
        return {"source": src.name, "error": git_error_class(e)}
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
        "git_commit=excluded.git_commit, git_commit_date=excluded.git_commit_date, "
        "config_scope=excluded.config_scope",
        (src.name, commit, commit_date, config_scope(src)))
    conn.commit()
    # A fresh index makes any cached verdict for this source+ref stale, whichever scope it
    # was memoized under — the key now includes config_scope (see freshness), so a plain
    # tuple lookup would miss.
    for key in [k for k in freshness_cache if k[0] == src.name and k[1] == src.ref]:
        freshness_cache.pop(key, None)

    # Now run the file-level indexing (shared core with dir sources)
    summary = {"source": src.name, "commit": commit, "commit_date": commit_date}
    if src.path is not None and src.ref:
        try:
            git_ops.resolve_ref(repo_path, src.ref)
        except RuntimeError:
            # Read-only mode cannot validate the ref by checking it out — surface
            # the typo where the work happens, not only in list_sources' 'unknown'.
            summary["warning"] = (
                f"ref '{src.ref}' does not resolve in the clone — check the ref "
                f"spelling in the manifest, or the ref may not be fetched into the "
                f"clone yet (run 'git fetch --tags' there); list_sources reports "
                f"'unknown'.")
    result = ingest(conn, src, repo_path, staging.scan(src, repo_path))
    summary.update(as_summary(result))
    summary["warnings"] = staging.report_all(conn, src, result)
    return summary


def reindex_source(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild one source. Dispatches on type: git_repo sources go through the
    managed flow (fetch, dirty-check, checkout) or the linked flow (verify repo,
    remote URL, clean tree only — read-only); dir sources index in place. Returns a summary.

    extraction_skips = files_seen - unique contents that required fresh extraction this run.
    Covers two cases: byte-identical duplicates within the same run (same sha, only one job
    submitted) and contents already extracted in a prior run (cache hits in `contents`).
    Value: files_seen - len(need), where need = unique shas not yet in contents.
    """
    if src.type == "git_repo":
        return reindex_git_repo(conn, src)
    summary = {"source": src.name, **as_summary(EMPTY_INDEXED)}
    if src.path is None or not src.path.exists():
        return summary
    result = ingest(conn, src, src.path, staging.scan(src, src.path))
    summary.update(as_summary(result))
    summary["warnings"] = staging.report_all(conn, src, result)
    return summary


STALENESS_TTL = 30  # seconds: git_repo verdicts are memoized in-process for this long


class Freshness(NamedTuple):
    """A freshness handler's verdict, and what it had to look at to reach it. `scan` is None
    for a handler that proves freshness without traversing (git compares commits); `changed`
    bounds an inline auto-reindex; `cached` means the verdict came from the memo (ADR 0015)."""
    changed: int
    stale: bool
    cached: bool
    scan: staging.Scan | None


def freshness(conn: sqlite3.Connection, src: Source) -> Freshness:
    """Freshness (CONTEXT.md): does the index still match this source? A handler table, one
    entry per source type (ADR 0015).

    git_repo verdicts cost git subprocesses (and a fetch for managed sources) and
    feed the auto-reindex attempt and its failure warning — both are memoized per
    source+ref+config_scope for STALENESS_TTL seconds (ADR 0013): the check runs at
    most every 30 s, and search re-attempts a stale source at most every 30 s, so a
    persistently broken or dirty source warns on the first query of each window,
    not on every query. The scope is part of the key, not just of the verdict
    computation: a manifest edit changes the key, so it can never hit a memo entry
    written under the old scope (ADR 0014) — the source may briefly hold two entries
    (old scope, new scope) but the old one is simply never looked up again.
    """
    if src.type == "git_repo":
        now = time.monotonic()
        key = (src.name, src.ref, config_scope(src))
        hit = freshness_cache.get(key)
        if hit and now - hit[0] < STALENESS_TTL:
            return Freshness(0, hit[1], True, None)
        _, stale = repo_freshness(conn, src)
        freshness_cache[key] = (now, stale)
        return Freshness(0, stale, False, None)
    return dir_freshness(conn, src)


def dir_freshness(conn: sqlite3.Connection, src: Source) -> Freshness:
    """Bookkeeping freshness: a (relpath, mtime) compare — no hashing, no extraction. The
    stat it needs is step 1's, so it scans, decides from that scan, and returns it for the
    ingest to re-use rather than walking a second time (ADR 0015)."""
    if src.path is None or not src.path.exists():
        return Freshness(0, False, False, None)
    indexed = {r[0]: r[1] for r in conn.execute(
        "SELECT relpath, mtime FROM locations WHERE source=?", (src.name,))}
    scan = staging.scan(src, src.path)
    on_disk = staging.still_present(indexed, scan, src.path)
    changed = sum(1 for rel, mtime in on_disk.items() if indexed.get(rel) != mtime)
    # An empty selection reads as fresh, which is correct: nothing to index, nothing to
    # purge. The forced-stale clause that used to carry its report is gone (ADR 0014).
    return Freshness(changed, changed > 0 or set(indexed) != set(on_disk), False, scan)


freshness_cache: dict[tuple[str, str | None, str], tuple[float, bool]] = {}


def config_scope(src: Source) -> str:
    """The normalized scope keys as one readable, unambiguous string.

    JSON rather than a delimiter-joined string: on Linux any byte but "/" and NUL is legal in a
    filename, so a directory named "a,b" and the pair ("a", "b") joined on a comma produce the
    same text — and a git_repo source edited between those two scopes reads as fresh, which is
    the exact failure this column exists to catch. Sorted, so order-independence comes from
    sorting rather than from hashing, and the column stays readable when someone inspects the
    index by hand (ADR 0014).
    """
    return json.dumps({
        "paths": sorted(src.paths),
        "exclude": sorted(src.exclude_paths),
        "allow": None if src.ext_allowlist is None else sorted(src.ext_allowlist),
        "block": None if src.ext_blocklist is None else sorted(src.ext_blocklist),
    }, separators=(",", ":"))


def repo_freshness(conn: sqlite3.Connection, src: Source) -> tuple[int, bool]:
    """Commit-based staleness for git_repo. Returns (0, stale).

    Linked mode is read-only (ADR 0013): rtfm never fetches the user's clone, so the
    refreshable reality is the user's tree — stale iff the indexed commit is not the
    current HEAD (the user moved their checkout) or the tree is dirty (uncommitted
    edits would be silently absent from results; a dirty reindex refusal then warns
    loudly on search). Remote movement alone is the user's own business (their fetch
    told them); list_sources reports it. A linked pinned SHA is the user's checkout
    concern — never auto-reindex (staleness undefined, ADR 0013).
    Managed clones are rtfm's own: fetch, then compare the indexed commit to
    origin/<ref> — for a pinned SHA, to the pin itself (the pin never moves, but the
    MANIFEST can: a changed pin must be detected, not served silently forever).
    """
    try:
        row = conn.execute(
            "SELECT git_commit, config_scope FROM source_meta WHERE source=?",
            (src.name,)).fetchone()
        if row is None:
            return 0, True  # never indexed — always stale
        if row[1] != config_scope(src):
            return 0, True  # the manifest moved; a pin freezes the commit, not the scope
        repo_path = src.path if src.path is not None else managed_repo_path(src.name)
        if not repo_path.exists():
            return 0, True  # clone vanished
        ref = src.ref or default_branch(repo_path)
        if src.path is not None:
            if git_ops.is_sha_pin(repo_path, ref):
                return 0, False  # linked pin: never auto-reindex (ADR 0013)
            head_moved = row[0] != git_ops.current_commit(repo_path)
            if head_moved:
                return 0, True
            clean, _ = git_ops.is_clean(repo_path)
            return 0, not clean  # dirty tree: reindex refuses loudly, search warns
        git_ops.fetch(repo_path)
        current = git_ops.resolve_ref(repo_path, ref)
    except RuntimeError:
        return 0, True  # graceful degradation: git failure means stale (will warn)
    return 0, (row[0] != current)


def default_branch(path: Path) -> str:
    """Return the remote's default branch name as the clone knows it (origin's HEAD
    symbolic ref), 'main' as fallback. Raises when the repo has no origin at all
    (callers classify NO_REMOTE). Purely local — `git remote show origin` does a
    network round-trip, which would stall list_sources offline and touch the user's
    remote in linked mode."""
    if not git_ops.remote_url(path):
        raise RuntimeError(f"{path} has no 'origin' remote")
    try:
        cp = git_ops.run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=path, timeout=10)
        return cp.stdout.strip().removeprefix("refs/remotes/origin/")
    except RuntimeError:
        return "main"  # sensible fallback


