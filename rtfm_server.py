#!/usr/bin/env python3
"""rtfm — Read The Full Manual. Local document-corpus search MCP server."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat as stat_mod
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP

from rtfm import config, git_ops
from rtfm.config import (
    CHUNK_LINES,
    DEFAULT_EXT_BLOCKLIST,
    MARKUP_EXTS,
    MAX_LOCATIONS,
    SCHEMA_VERSION,
    corpus_home,
    index_db_path,
    managed_repo_path,
    manifest_path,
)
from rtfm.manifest import (
    Source,
    ensure_bootstrap,
    load_manifest,
    normalize_exts,
    normalize_paths,
    source_from_table,
    validate_source,
)

# --- config -----------------------------------------------------------------
_log = logging.getLogger("rtfm")

# Temporary alias for the not-yet-moved tests; the package path is the source
# of truth and the facade is trimmed in the final task.
_managed_repo_path = managed_repo_path
default_source_dir = config.default_source_dir

# --- git operations ----------------------------------------------------------

# Temporary facade aliases: not-yet-moved sections and tests still call these
# names. The owning module is rtfm.git_ops; each retarget happens as its section
# moves out of this file.
_git = git_ops.run
_git_repo_root = git_ops.repo_root
_git_remote_url = git_ops.remote_url
_git_is_clean = git_ops.is_clean
_git_current_commit = git_ops.current_commit
_git_commit_date = git_ops.commit_date
_git_resolve_ref = git_ops.resolve_ref
_git_fetch = git_ops.fetch
_git_clone = git_ops.clone
_git_checkout = git_ops.checkout
_is_sha = git_ops.is_sha
_is_sha_pin = git_ops.is_sha_pin
_git_is_branch = git_ops.is_branch
_git_is_ancestor = git_ops.is_ancestor


def _linked_git_status(repo_path: Path, src: Source, indexed: str) -> str:
    """git_status for a linked clone, in git's own terms — read-only: compares the
    indexed commit against the LOCAL origin/<ref> (origin first, local fallback for
    tags/SHAs/local-only branches — the clone's own knowledge; rtfm never fetches a
    linked clone, ADR 0013). A pinned SHA puts the checkout in git's detached-HEAD
    state — staleness undefined. An unresolvable ref means 'unknown' — check the ref
    spelling in the manifest."""
    try:
        ref = src.ref or _default_branch(repo_path)
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


def _managed_git_status(repo_path: Path, src: Source, indexed: str) -> str:
    """git_status for a managed clone: fetch (rtfm owns the clone), then compare the
    indexed commit to origin/<ref> (origin first, local fallback). The reset keeps
    HEAD at origin, so 'ahead' and 'diverged' are unreachable here. A pinned SHA
    needs no fetch — the pin never moves — and reports git's detached-HEAD state."""
    try:
        ref = src.ref or _default_branch(repo_path)
        if git_ops.is_sha_pin(repo_path, ref):
            return "detached"
        git_ops.fetch(repo_path)
        current = git_ops.resolve_ref(repo_path, ref)
    except RuntimeError:
        return "unknown"
    if indexed == current:
        return "up to date"
    return "behind"

# --- index ------------------------------------------------------------------

def get_index_db() -> sqlite3.Connection:
    p = index_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
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


def _workers() -> int:
    env = os.environ.get("RTFM_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return min(os.cpu_count() or 4, 8)


AUTO_REINDEX_MAX_FILES = 10          # inline new/changed-file budget on query (0 = none)


def _auto_reindex_max() -> int:
    """Max new/changed files a source may have for `search` to reindex it inline. Bounds query
    latency (PDF extraction is the cost); larger deltas fall back to a 'run reindex' warning.
    RTFM_AUTO_REINDEX_MAX overrides; 0 disables inline reindexing of new/changed files (files
    that vanished on disk are still purged inline — that costs nothing)."""
    env = os.environ.get("RTFM_AUTO_REINDEX_MAX")
    if env and env.isdigit():
        return int(env)
    return AUTO_REINDEX_MAX_FILES


def _pdf_rows(path: Path) -> list[tuple[str, str, str]]:
    """Page locators, one row per page with text (ADR 0004)."""
    rows: list[tuple[str, str, str]] = []
    for page_num, page_text in enumerate(extract_pdf_text(path).split("\f"), 1):
        content = "\n".join(ln.strip() for ln in page_text.splitlines() if ln.strip())
        if content:
            rows.append(("page", str(page_num), content))
    return rows


def _text_rows(path: Path) -> list[tuple[str, str, str]]:
    """Line locators, CHUNK_LINES per row (ADR 0004)."""
    rows: list[tuple[str, str, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i in range(0, max(1, len(lines)), CHUNK_LINES):
        chunk = "\n".join(ln.rstrip() for ln in lines[i:i + CHUNK_LINES])
        if chunk.strip():
            rows.append(("line", str(i + 1), chunk))
    return rows


def _rows_for_file(path: Path) -> list[tuple[str, str, str]]:
    """Return (locator_kind, locator_value, text) rows for a file, via its routine."""
    return _routine_for(path.suffix.lower()).rows(path)


def _sane_title(s: str) -> bool:
    """Reject empty / too-short / obvious authoring-tool artifacts so junk metadata titles don't
    outrank real ones."""
    s = (s or "").strip()
    if len(s) < 3:
        return False
    low = s.lower()
    return not (low.endswith((".pdf", ".doc", ".docx")) or "microsoft word" in low)


def _first_substantial_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) >= 4 and not line.isdigit():
            return line
    return ""


def _pdf_doc_signal(path: Path) -> tuple[str, str]:
    """(title, headings) for a PDF. Title = sane metadata title, else first substantial page-1
    line. Headings = the bookmark outline (doc.get_toc) — which survives an image front-page,
    the failure mode where the visible title isn't in the extracted text."""
    import fitz
    doc = fitz.open(str(path))
    try:
        meta = (doc.metadata or {}).get("title") or ""
        headings = "\n".join(t for (_lvl, t, _pg) in (doc.get_toc() or []) if t)
        title = meta.strip() if _sane_title(meta) else _first_substantial_line(
            doc[0].get_text() if doc.page_count else "")
    finally:
        doc.close()
    return title, headings


def _text_doc_signal(path: Path) -> tuple[str, str]:
    """(title, headings) for markup. ATX (`#`) and setext/rst underline headings; title = first.

    Contract for a setext/rst underline: a homogeneous run of one underline char (`= - ~ ^`,
    length ≥ 3) directly under a non-blank line makes that line a heading — CommonMark-aligned
    (`paragraph\\n---` is a heading; a `---` after a blank line is a horizontal rule, not a
    heading). A leading `---` YAML frontmatter block is skipped first so its closing fence isn't
    misread as an underline (which would make a frontmatter key the title)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                start = j + 1
                break
    headings: list[str] = []
    for i in range(start, len(lines)):
        line = lines[i].strip()
        if line.startswith("#"):
            headings.append(line.lstrip("#").strip())
        elif (len(line) >= 3 and len(set(line)) == 1 and line[0] in "=-~^"
              and i > start and lines[i - 1].strip()):       # homogeneous setext/rst underline
            headings.append(lines[i - 1].strip())
    headings = [h for h in headings if h]
    return (headings[0] if headings else ""), "\n".join(headings)


def _no_doc_signal(_path: Path) -> tuple[str, str]:
    """No title/heading signal. A file whose format rtfm has no structural reader for is
    body-searchable only — guessing structure from '#' comment lines would put a source
    file's licence header into doc-level ranking (ADR 0012)."""
    return "", ""


class _Routine(NamedTuple):
    """One handling routine: how a family of files becomes rows and a doc-level signal.

    Body and signal live in the same record so the two dispatches cannot disagree about
    which routine a file belongs to. Adding a format is adding a routine."""
    name: str
    exts: frozenset[str]
    rows: Callable[[Path], list[tuple[str, str, str]]]
    signal: Callable[[Path], tuple[str, str]]


ROUTINES: tuple[_Routine, ...] = (
    _Routine("pdf", frozenset({".pdf"}), _pdf_rows, _pdf_doc_signal),
    _Routine("markup", MARKUP_EXTS, _text_rows, _text_doc_signal),
)

# The fallback sits outside ROUTINES. Inside, it would match unconditionally, so placing
# it anywhere but last would silently shadow every routine after it — .pdf quietly getting
# plain-text rows and no signal, with nothing raised. Out here there is no ordering to get
# wrong.
DEFAULT_ROUTINE = _Routine("text", frozenset(), _text_rows, _no_doc_signal)


def _routine_for(ext: str) -> _Routine:
    """The routine handling `ext`, else the plain-text fallback."""
    return next((r for r in ROUTINES if ext in r.exts), DEFAULT_ROUTINE)


def _doc_signal_for_file(path: Path) -> tuple[str, str]:
    """Doc-level (title, headings) for ranking. Best-effort — a failure yields ("", "") so the
    document still ranks on body text and signal extraction never blocks body extraction. The
    failure is logged (not silent): a broken/absent pymupdf would otherwise strip title/heading
    ranking corpus-wide with nothing to grep."""
    try:
        return _routine_for(path.suffix.lower()).signal(path)
    except ImportError as e:
        _log.warning("doc-signal disabled for %s (%s) — body search unaffected; reinstall to "
                     "restore title/heading ranking", path, e)
    except Exception as e:
        _log.warning("doc-signal extraction failed for %s: %s", path, e, exc_info=True)
    return "", ""


def _extract_rows(path_str: str) -> tuple[list[tuple[str, str, str]], str, str, str | None]:
    """Extraction worker, run in a thread by _extract_many: extract (rows, title, headings) for
    one file. Returns (rows, title, headings, error); a failed body extraction yields
    ([], "", "", message) instead of raising."""
    try:
        path = Path(path_str)
        return _rows_for_file(path), *_doc_signal_for_file(path), None
    except Exception as e:
        return [], "", "", f"{type(e).__name__}: {e}"


class _Extracted(NamedTuple):
    """One content's extraction result. Named so the two adjacent strings (title, headings) can't
    be swapped by positional unpacking downstream."""
    sha: str
    rows: list
    title: str
    headings: str
    error: str | None


class Problem(NamedTuple):
    """One thing that went wrong, attributed to the position it happened to.

    `position` is a relpath and always a relpath — never a content hash. A sha is an
    internal identity a user cannot act on, and reporting one forces every consumer to
    convert content counts into file counts. One unit everywhere means nothing converts
    (ADR 0015)."""
    position: str
    reason: str


class StepResult(NamedTuple):
    """What a step let through, and what it could not.

    The happy/partial/empty classification is derivable from these two, so it is a
    function of a result rather than a field on it. `kept` means what this step passes
    to the next stage — what survived for most steps, but for extraction (step 3B), both
    successful and failed results that must persist (2026-09-16 four-step indexing
    design doc)."""
    kept: list
    problems: list[Problem]


def _is_clean(r: StepResult) -> bool:
    return bool(r.kept) and not r.problems


def _is_partial(r: StepResult) -> bool:
    return bool(r.kept) and bool(r.problems)


def _is_empty(r: StepResult) -> bool:
    return not r.kept


class _Reached(NamedTuple):
    """Step 1's product. The mtime is step 1's own stat — the freshness fact, recorded where
    it is already paid for rather than re-fetched by a second walk (ADR 0015)."""
    path: Path
    mtime: float


class _Hashed(NamedTuple):
    """Step 3A's product: a position that gave up its bytes, and their hash."""
    path: Path
    sha: str
    mtime: float


class _Scan(NamedTuple):
    """Steps 1 and 2 together. One value because its two consumers — the freshness handler
    that decides and the ingest that acts — must see the same walk (ADR 0015)."""
    reachable: StepResult
    wanted: list[_Reached]


class CacheStats(NamedTuple):
    """Telemetry about the content-addressed store (ADR 0010), not about failures.
    Kept apart from step results so a cache detail can never reach a user-facing
    report."""
    unique_contents: int
    newly_extracted: int
    extraction_skips: int
    purged: int


class Indexed(NamedTuple):
    """One indexing run. `wanted` is a bare list, not a StepResult: step 2 asks about
    intent, where rtfm has no standing to call 90% filtered a partial success, so there
    is nowhere to record a step-2 partial outcome (ADR 0015)."""
    reachable: StepResult      # step 1  — can rtfm get at these bytes
    wanted: list               # step 2  — which of them does the manifest want
    read: StepResult           # step 3A — which gave up their bytes
    handled: StepResult        # step 3B — which became rows
    cache: CacheStats


_STEP_MESSAGES = {
    "access": ("COULD NOT OPEN", "could not be reached",
               "fix the permissions, or point the source elsewhere in {manifest}"),
    "read":   ("COULD NOT READ", "could not give up their bytes",
               "fix storage or permissions where the source lives"),
    "handle": ("COULD NOT HANDLE", "could not be processed",
               "convert them, or exclude their file types in {manifest}"),
}


def report(src_name: str, result: StepResult, step: str) -> list[str]:
    """Turn one step's problems into messages. The only place a Problem becomes text.

    Each step names itself and gives its own remedy, because merging them tells a user to
    convert a file they simply lack permission to open (ADR 0015). The manifest is named
    only where editing it is the actual fix — step 3A's failure is infrastructure (a failing
    disk, a permission bit), and telling someone to fix that by editing TOML is the
    merged-step defect ADR 0015 deletes, wearing a different hat."""
    if not result.problems:
        return []
    tag, what, recover = _STEP_MESSAGES[step]
    shown = ", ".join(p.position for p in result.problems[:3])
    more = f" (+{len(result.problems) - 3} more)" if len(result.problems) > 3 else ""
    distinct = sorted({p.reason for p in result.problems})
    reasons = ", ".join(distinct[:2])
    if len(distinct) > 2:
        reasons += f" (+{len(distinct) - 2} more)"
    return [f"!!! {tag} '{src_name}' !!! {len(result.problems)} path(s) {what}: "
            f"{shown}{more} — {reasons}. "
            f"Recover: {recover.format(manifest=manifest_path())}."]


def _report_nothing_selected(src: Source, result: Indexed) -> list[str]:
    """Step 2's sad path (ADR 0015): step 1 reached something and step 2 kept none of it.

    Derived here from `Indexed`, not stored on a `StepResult` — step 2 returns a bare list on
    purpose (see `select`): rtfm has no standing to call 90% filtered a partial success, so
    there is nowhere for a step-2 outcome to live except this comparison of two already-stored
    results. `reachable.kept` empty is step 1's sad path, not this one — the access report (or,
    for a merely-empty directory, nothing at all) already says everything there is to say, so
    the condition below requires step 1 to have reached something."""
    if not (result.reachable.kept and not result.wanted):
        return []
    if src.ext_allowlist is not None:
        ext_clause = f"ext_allowlist = {', '.join(sorted(src.ext_allowlist)) or '(empty)'}"
    else:
        # The effective set is the user's list unioned with rtfm's own defaults, but dumping
        # all of it is a wall of extensions the user never wrote and cannot act on, burying
        # the `paths` clause that is often the actual cause. Name what the user declared and
        # summarize the defaults by count — never hardcoded, so it can't drift when
        # DEFAULT_EXT_BLOCKLIST grows.
        n_defaults = len(DEFAULT_EXT_BLOCKLIST)
        user = sorted(src.ext_blocklist or frozenset())
        if user:
            ext_clause = (f"ext_blocklist = {', '.join(user)} "
                          f"(plus rtfm's {n_defaults} default binary and asset types)")
        else:
            ext_clause = (f"ext_blocklist = [] "
                          f"(rtfm's {n_defaults} default binary and asset types only)")
    parts = list(src.paths) + [f"!{p}" for p in src.exclude_paths]
    paths_clause = f" — and paths = {', '.join(parts)}" if parts else ""
    return [f"!!! NOTHING SELECTED '{src.name}' !!! {len(result.reachable.kept):,} path(s) "
            f"reachable, none selected. {ext_clause}{paths_clause}. "
            f"Recover: widen the extension list or the paths in {manifest_path()}."]


def report_unsearchable(conn: sqlite3.Connection, src_name: str) -> list[str]:
    """What cannot be searched here because its content failed extraction. A question about
    the store, so a caller that ran no pipeline can still ask it; the join fans a failed sha
    out over every position it lives at (ADR 0015)."""
    return report(src_name, StepResult([], [
        Problem(relpath, error or "extraction failed in an earlier run")
        for relpath, error in conn.execute(
            "SELECT l.relpath, c.error FROM locations l JOIN contents c "
            "ON c.sha256 = l.sha256 WHERE l.source = ? AND c.extracted_ok = 0 "
            "ORDER BY l.relpath", (src_name,))]), "handle")


def report_all(conn: sqlite3.Connection, src: Source, result: Indexed) -> list[str]:
    """Every step's report, in pipeline order. Step 3B's line comes from the store, not from
    `result.handled`, so a run that indexed nothing still names what is broken and the three
    callers cannot drift."""
    return (report(src.name, result.reachable, "access")
            + _report_nothing_selected(src, result)
            + report(src.name, result.read, "read")
            + report_unsearchable(conn, src.name))


def _extract_many(jobs: list[tuple[str, str]]) -> list[_Extracted]:
    """jobs: [(sha, path)] -> [_Extracted(sha, rows, title, headings, error)]. Parallel over unique
    contents (dedup has already cut the job count) with a THREAD pool. A process pool is
    unsafe here: this runs inside the FastMCP server, whose sync tools execute on a worker
    thread, so a fork-based ProcessPoolExecutor deadlocks — forked workers inherit locks held
    by threads that don't exist in the child and hang at startup (0% CPU, server wedged). This
    is invisible to pytest (single-threaded). Threads carry the common case anyway: extraction
    is dominated by the `pdftotext` subprocess, which releases the GIL. Tradeoff vs. a process
    pool: a hard crash in a worker (e.g. a segfault in a native PDF extractor) takes down the
    whole server here, where a process pool would have contained it — acceptable because the hot
    path is the isolated `pdftotext` subprocess and the pure-Python fallbacks rarely segfault; a
    `spawn`-context process pool would regain isolation without the fork hazard if that changes.
    Workers = RTFM_WORKERS or min(cpus, 8)."""
    if not jobs:
        return []
    if len(jobs) == 1 or _workers() == 1:
        return [_Extracted(sha, *_extract_rows(path)) for (sha, path) in jobs]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: list[_Extracted] = []
    with ThreadPoolExecutor(max_workers=_workers()) as ex:
        futs = {ex.submit(_extract_rows, path): sha for (sha, path) in jobs}
        for fut in as_completed(futs):
            out.append(_Extracted(futs[fut], *fut.result()))
    return out


def _git_missing_error() -> str:
    """Classified error for a missing git binary — without the check, a machine
    without git would be misreported as NOT_GIT_REPO."""
    return ("ERROR:GIT_MISSING: git executable not found. "
            "Recover: install git, or remove the git_repo source.")


def _git_error_class(e: RuntimeError) -> str:
    """Classified error for a git failure that isn't one of the known classes:
    GIT_MISSING for an absent binary, GIT_FAILED for everything else (corruption,
    permissions, ownership guard, timeout) — never misreported as 'not a repo'."""
    if "git executable not found" in str(e):
        return _git_missing_error()
    return (f"ERROR:GIT_FAILED: {e}. "
            "Recover: fix the repo's git state (permissions, ownership, corruption) "
            "and try again.")


def _dirty_error(repo_path: Path) -> str | None:
    """Classified DIRTY_TREE error if the working tree has changes, else None."""
    clean, dirty_files = git_ops.is_clean(repo_path)
    if not clean:
        return (
            f"ERROR:DIRTY_TREE: {len(dirty_files)} file(s) modified or untracked — "
            f"{', '.join(dirty_files[:10])}. "
            f"Recover: commit, stash, or clean the working tree, then reindex.")
    return None


def _ensure_git_repo_ready(src: Source) -> tuple[Path, str | None]:
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
            return repo_path, _git_error_class(e)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} is not a git repository. "
                f"Recover: clone the repo or remove 'path' from the manifest.")
        if src.url:
            try:
                remote = git_ops.remote_url(root)
            except RuntimeError as e:
                return repo_path, _git_error_class(e)
            if remote and git_ops.normalize_remote(remote) != git_ops.normalize_remote(src.url):
                return repo_path, (
                    f"ERROR:REMOTE_MISMATCH: origin is '{remote}', manifest declares "
                    f"'{src.url}'. Recover: fix the manifest or update the clone's origin.")
        try:
            error = _dirty_error(repo_path)
        except RuntimeError as e:
            return repo_path, _git_error_class(e)
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
                return repo_path, _git_missing_error()
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
            return repo_path, _git_error_class(e)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} exists but is not a git repository. "
                f"Recover: remove {repo_path} and reindex.")
        try:
            remote = git_ops.remote_url(repo_path)
        except RuntimeError as e:
            return repo_path, _git_error_class(e)
        if (remote and src.url
                and git_ops.normalize_remote(remote) != git_ops.normalize_remote(src.url)):
            return repo_path, (
                f"ERROR:REMOTE_MISMATCH: managed clone at {repo_path} has origin "
                f"'{remote}', manifest declares '{src.url}'. "
                f"Recover: remove {repo_path} and reindex, or fix the manifest.")

    # Ref to use
    try:
        ref = src.ref or _default_branch(repo_path)
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
        error = _dirty_error(repo_path)
    except RuntimeError as e:
        return repo_path, _git_error_class(e)
    if error:
        return repo_path, error

    try:
        git_ops.checkout(repo_path, ref)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:CHECKOUT_FAILED: ref '{ref}' — {e}. "
            f"Recover: verify the ref exists on the remote and try again.")

    return repo_path, None


def _ingest(conn: sqlite3.Connection, src: Source, root: Path, scan: _Scan) -> Indexed:
    """Steps 3A and 3B over an already-scanned source, and sync the index. Takes the `_Scan`
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

    read = read_bytes_for(scan.wanted, root, existing)

    # A position that failed 3A is still in `wanted` (read_bytes_for reports it as a problem,
    # not by omission), so it keeps its row. A de-selected file is present on disk and must
    # still be purged: presence is step 2's answer, not the filesystem's.
    vanished = set(existing) - set(_still_present(
        {rel: mtime for rel, (_sha, mtime) in existing.items()}, scan, root))
    for rel in vanished:
        conn.execute("DELETE FROM locations WHERE source=? AND relpath=?", (source_name, rel))
    for h in read.kept:
        conn.execute(
            "INSERT INTO locations(source, relpath, sha256, mtime) VALUES(?,?,?,?) "
            "ON CONFLICT(source, relpath) DO UPDATE SET "
            "sha256=excluded.sha256, mtime=excluded.mtime",
            (source_name, _rel(h.path, root), h.sha, h.mtime))
    conn.commit()

    shas_here = {h.sha for h in read.kept}
    already = {r[0] for r in conn.execute("SELECT sha256 FROM contents")}
    need = shas_here - already
    handled = handle(read.kept, need, root)

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


def _as_summary(result: Indexed) -> dict:
    """Today's counter dict, as a projection of `Indexed`. The JSON wire format does not move."""
    errors = sum(1 for ex in result.handled.kept if ex.error)
    return {"files_seen": len(result.wanted),
            "unique_contents": result.cache.unique_contents,
            "newly_extracted": result.cache.newly_extracted,
            "extraction_skips": result.cache.extraction_skips,
            "purged": result.cache.purged,
            "errors": errors}


_EMPTY_INDEXED = Indexed(reachable=StepResult([], []), wanted=[],
                         read=StepResult([], []), handled=StepResult([], []),
                         cache=CacheStats(0, 0, 0, 0))


def _reindex_git_repo(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild a git_repo source."""
    repo_path, error = _ensure_git_repo_ready(src)
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
        return {"source": src.name, "error": _git_error_class(e)}
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
        "git_commit=excluded.git_commit, git_commit_date=excluded.git_commit_date, "
        "config_scope=excluded.config_scope",
        (src.name, commit, commit_date, _config_scope(src)))
    conn.commit()
    # A fresh index makes any cached verdict for this source+ref stale, whichever scope it
    # was memoized under — the key now includes config_scope (see _freshness), so a plain
    # tuple lookup would miss.
    for key in [k for k in _freshness_cache if k[0] == src.name and k[1] == src.ref]:
        _freshness_cache.pop(key, None)

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
    result = _ingest(conn, src, repo_path, _scan(src, repo_path))
    summary.update(_as_summary(result))
    summary["warnings"] = report_all(conn, src, result)
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
        return _reindex_git_repo(conn, src)
    summary = {"source": src.name, **_as_summary(_EMPTY_INDEXED)}
    if src.path is None or not src.path.exists():
        return summary
    result = _ingest(conn, src, src.path, _scan(src, src.path))
    summary.update(_as_summary(result))
    summary["warnings"] = report_all(conn, src, result)
    return summary


STALENESS_TTL = 30  # seconds: git_repo verdicts are memoized in-process for this long


class _Freshness(NamedTuple):
    """A freshness handler's verdict, and what it had to look at to reach it. `scan` is None
    for a handler that proves freshness without traversing (git compares commits); `changed`
    bounds an inline auto-reindex; `cached` means the verdict came from the memo (ADR 0015)."""
    changed: int
    stale: bool
    cached: bool
    scan: _Scan | None


def _freshness(conn: sqlite3.Connection, src: Source) -> _Freshness:
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
        key = (src.name, src.ref, _config_scope(src))
        hit = _freshness_cache.get(key)
        if hit and now - hit[0] < STALENESS_TTL:
            return _Freshness(0, hit[1], True, None)
        _, stale = _repo_freshness(conn, src)
        _freshness_cache[key] = (now, stale)
        return _Freshness(0, stale, False, None)
    return _dir_freshness(conn, src)


def _dir_freshness(conn: sqlite3.Connection, src: Source) -> _Freshness:
    """Bookkeeping freshness: a (relpath, mtime) compare — no hashing, no extraction. The
    stat it needs is step 1's, so it scans, decides from that scan, and returns it for the
    ingest to re-use rather than walking a second time (ADR 0015)."""
    if src.path is None or not src.path.exists():
        return _Freshness(0, False, False, None)
    indexed = {r[0]: r[1] for r in conn.execute(
        "SELECT relpath, mtime FROM locations WHERE source=?", (src.name,))}
    scan = _scan(src, src.path)
    on_disk = _still_present(indexed, scan, src.path)
    changed = sum(1 for rel, mtime in on_disk.items() if indexed.get(rel) != mtime)
    # An empty selection reads as fresh, which is correct: nothing to index, nothing to
    # purge. The forced-stale clause that used to carry its report is gone (ADR 0014).
    return _Freshness(changed, changed > 0 or set(indexed) != set(on_disk), False, scan)


_freshness_cache: dict[tuple[str, str | None, str], tuple[float, bool]] = {}


def _config_scope(src: Source) -> str:
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


def _repo_freshness(conn: sqlite3.Connection, src: Source) -> tuple[int, bool]:
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
        if row[1] != _config_scope(src):
            return 0, True  # the manifest moved; a pin freezes the commit, not the scope
        repo_path = src.path if src.path is not None else managed_repo_path(src.name)
        if not repo_path.exists():
            return 0, True  # clone vanished
        ref = src.ref or _default_branch(repo_path)
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


def _default_branch(path: Path) -> str:
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


def select(positions: list[_Reached], base: Path, src: Source) -> list[_Reached]:
    """Step 2 (ADR 0015): out of what we can reach, what does the user want?

    Policy, not a handler table — the manifest's `paths` prefixes and its one extension
    list (ADR 0014). No content is inspected, ever: a file outside the lists was never a
    candidate, which is not an error and is reported nowhere.

    Returns a bare list, deliberately, not a StepResult. Steps 1, 3A and 3B ask about the
    world, where "partial" is an observed fact worth reporting. This step asks about
    intent, and rtfm has no standing to call 90% filtered a partial success — either
    something survived or nothing did. With no `problems` field there is nowhere to write
    a step-2 partial-filtering warning, so nobody can.
    """
    if src.ext_allowlist is not None:
        def wanted_ext(ext: str) -> bool:
            return ext in src.ext_allowlist
    else:
        denied = DEFAULT_EXT_BLOCKLIST | (src.ext_blocklist or frozenset())

        def wanted_ext(ext: str) -> bool:
            return ext not in denied

    def wanted_path(rel: str) -> bool:
        if any(_under(rel, p) for p in src.exclude_paths):
            return False
        return not src.paths or any(_under(rel, p) for p in src.paths)

    return [r for r in positions
            if wanted_ext(r.path.suffix.lower()) and wanted_path(_rel(r.path, base))]


def _under(rel: str, prefix: str) -> bool:
    """Is `rel` at or beneath this directory prefix? "docs" must not match "docsets/x.md"."""
    return rel == prefix or rel.startswith(prefix + "/")


def access(src: Source, root: Path | None = None) -> StepResult:
    """Step 1 (ADR 0015): can rtfm get at these bytes?

    Enumerates the source and scans what it finds for readability. `root` overrides `src.path`
    for a managed git_repo, whose tree lives at its clone.

    `os.walk` with an `onerror` callback, not `rglob`: pathlib catches OSError inside scandir
    and yields nothing, which is indistinguishable from an empty directory, so a subtree this
    process cannot enter would vanish with nothing raised and nothing to report.

    Reports unfiltered — we cannot filter what we could not read. An inaccessible directory
    might hold exactly the files the user wants, and its contents are unknowable from outside,
    so letting step 2's intent suppress step 1's facts would leave a user writing a manifest
    against a corpus rtfm silently truncated.

    This scans; it does not read. `os.access` is a stat, and it may assert the negative but
    only ever suggest the positive — mode bits denying you is conclusive, mode bits allowing
    you is a hint, and step 3A owns the truth. The stat it already pays for leaves on each
    `_Reached`, so the freshness handler decides from this walk (ADR 0015)."""
    base = root if root is not None else src.path
    if base is None or not base.exists():
        return StepResult([], [])
    kept: list[_Reached] = []
    problems: list[Problem] = []

    def _denied(err: OSError) -> None:
        if err.filename:
            problems.append(Problem(
                _rel(Path(err.filename), base), err.strerror or type(err).__name__))

    for dirpath, dirnames, filenames in os.walk(base, onerror=_denied):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if not n.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            f = d / name
            try:
                st = f.stat()
            except OSError:
                continue          # `is_file()` swallowed these too: same silent skip
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            if os.access(f, os.R_OK):
                kept.append(_Reached(f, st.st_mtime))
            else:
                problems.append(Problem(_rel(f, base), "Permission denied"))
    return StepResult(sorted(kept), problems)


def _scan(src: Source, root: Path) -> _Scan:
    """Steps 1 and 2 over one source, and the only place `access` is called on the indexing
    path — so a query walks once, however many consumers the result has."""
    reachable = access(src, root)
    return _Scan(reachable, select(reachable.kept, root, src))


def _rel(p: Path, base: Path) -> str:
    """A position: the path as the user wrote it, relative to the source root."""
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return str(p)


def _still_present(indexed: dict[str, float], scan: _Scan, root: Path) -> dict[str, float]:
    """Positions this source still has, and their mtimes: what step 2 wants, plus indexed
    rows sitting behind something step 1 could not enter.

    Unreadable is not absent. A position denied by step 1 never reaches step 2, so asking
    step 2 alone would call it vanished, delete its row and GC its content because someone
    ran chmod. Freshness and the ingest both need this exact set — freshness to stop going
    stale forever, the ingest to stop purging — and one function is how they agree."""
    present = {_rel(r.path, root): r.mtime for r in scan.wanted}
    unreachable = {p.position for p in scan.reachable.problems}
    for rel, mtime in indexed.items():
        if rel in present or not _shadowed_by(rel, unreachable):
            continue
        try:
            present[rel] = (root / rel).stat().st_mtime
        except OSError:
            present[rel] = mtime      # behind an unreachable ancestor: can't re-read
    return present


def _shadowed_by(rel: str, unreachable: set[str]) -> bool:
    """Does a position step 1 could not reach sit on this relpath's way down?

    `_rel` renders the source root as ".", which is the one position no relpath equals or
    is prefixed by, so a denied root has to be matched on its own terms. Miss it and every
    indexed row of that source reads as vanished while every file is still on disk.

    Both reconcilers ask this, and they have to agree: index_source deletes rows on a False
    and _stale_delta stops converging on one.
    """
    return any(u == "." or rel == u or rel.startswith(u + "/") for u in unreachable)


def read_bytes_for(positions: list[_Reached], base: Path,
                   existing: dict[str, tuple[str, float]]) -> StepResult:
    """Step 3A (ADR 0015): are those bytes even readable?

    Step 1 scanned; this one actually reads. Stale NFS/CIFS handles, EIO, ACLs
    beyond the mode bits, a file moved underneath us between the scan and the
    read. Failing here is infrastructure — the user fixes storage or permissions,
    not files.

    `existing` lets an unchanged position skip re-hashing. It is a cache on the hash, never
    a gate on the read — and never a gate on the liveness check either: every position is
    stat'd here even on a cache hit, because a file deleted between step 1's scan and this
    step is precisely the race this step owns, and skipping the syscall would keep its row
    and its old sha with nothing reported.

    The mtime compared is step 1's, not this stat's: if a file changes between scan and read
    we store the older mtime against the new content, so the next run re-reads it once —
    never the reverse.

    PR2 widens the cache key from mtime to mtime+size."""
    kept: list[_Hashed] = []
    problems: list[Problem] = []
    for r in positions:
        rel = _rel(r.path, base)
        try:
            r.path.stat()                       # liveness: gone since the scan is step 3A's
            prev = existing.get(rel)
            sha = prev[0] if prev and prev[1] == r.mtime else \
                hashlib.sha256(r.path.read_bytes()).hexdigest()
        except OSError as e:
            problems.append(Problem(rel, e.strerror or type(e).__name__))
            continue
        kept.append(_Hashed(r.path, sha, r.mtime))
    return StepResult(kept, problems)


def handle(read_kept: list[_Hashed], need: set[str], base: Path) -> StepResult:
    """Step 3B (ADR 0015): can we handle those bytes?

    One handler per format, each reporting in its own terms — a corrupt PDF failed to parse
    and is not "invalid UTF-8". Decoding belongs inside the markup and plain-text handlers;
    the PDF handler reads bytes through a parser and never decodes. Failing here is the file:
    the user converts it or excludes its type.

    Extraction is per content (ADR 0010): N byte-identical positions are one job. But the
    problems returned are per POSITION, fanned out here at this boundary, so that a position
    is the only identity anywhere downstream and no consumer ever converts a content count
    into a file count.

    Reports what THIS run failed to handle, nothing else: what is currently unsearchable is a
    question about the store, and `report_unsearchable` owns it (ADR 0015)."""
    by_sha: dict[str, list[str]] = {}
    jobs: dict[str, str] = {}
    for h in read_kept:
        by_sha.setdefault(h.sha, []).append(_rel(h.path, base))
        if h.sha in need and h.sha not in jobs:
            jobs[h.sha] = str(h.path)

    kept: list[_Extracted] = []
    problems: list[Problem] = []
    for ex in _extract_many(list(jobs.items())):
        if ex.error:
            problems.extend(Problem(pos, ex.error) for pos in sorted(by_sha.get(ex.sha, [])))
        kept.append(ex)
    return StepResult(kept, sorted(problems))


# --- manifest ---------------------------------------------------------------

# Temporary facade aliases: not-yet-moved sections and tests still call these
# names. The owning module is rtfm.manifest.
_source_from_table = source_from_table
_validate_source = validate_source
_ensure_bootstrap = ensure_bootstrap
_normalize_paths = normalize_paths
_normalize_exts = normalize_exts

# --- extractors -------------------------------------------------------------

def _pdftotext(path: Path, start=None, end=None, timeout=60) -> str:
    cmd = ["pdftotext", "-layout"]
    if start is not None:
        cmd += ["-f", str(start)]
    if end is not None:
        cmd += ["-l", str(end)]
    cmd += [str(path), "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext exited {out.returncode}: {(out.stderr or '').strip()[:200]}")
    return out.stdout


def _pymupdf(path: Path, start=None, end=None) -> str:
    import fitz
    doc = fitz.open(str(path))
    texts = []
    for i in range(len(doc)):
        n = i + 1
        if start is not None and n < start:
            continue
        if end is not None and n > end:
            break
        texts.append(doc[i].get_text())
    return "\f".join(texts)


def _pypdf(path: Path, start=None, end=None) -> str:
    import pypdf
    reader = pypdf.PdfReader(str(path))
    texts = []
    for i, page in enumerate(reader.pages):
        n = i + 1
        if start is not None and n < start:
            continue
        if end is not None and n > end:
            break
        texts.append(page.extract_text() or "")
    return "\f".join(texts)


def extract_pdf_text(path: Path, start=None, end=None, timeout=60) -> str:
    """pdftotext -> pymupdf -> pypdf fallback chain. poppler is optional (ADR 0009)."""
    try:
        return _pdftotext(path, start, end, timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError):
        pass
    try:
        return _pymupdf(path, start, end)
    except ImportError:
        return _pypdf(path, start, end)

# --- tools ------------------------------------------------------------------

_SNIPPET_CAP = 600


def _sanitize_fts(query: str) -> str:
    # Replace every non-word, non-space char with a space so arbitrary user
    # punctuation degrades to keyword tokens and can never form invalid FTS5 syntax.
    return re.sub(r"[^\w\s]", " ", query).strip() or '""'


def _best_snippet(text: str, qterms: list[str]) -> str:
    """The line covering the most query terms (specs-server parity — beats first-match)."""
    best, best_score = "", -1
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        ll = line.lower()
        score = sum(1 for t in qterms if t in ll)
        if score > best_score:
            best, best_score = line, score
    return best


def _doc_title(conn: sqlite3.Connection, sha: str) -> str:
    row = conn.execute("SELECT title FROM doc_fts WHERE sha256=?", (sha,)).fetchone()
    return row[0] if row and row[0] else ""


def _locations_for(conn: sqlite3.Connection, sha: str,
                   max_locations: int) -> tuple[list[dict], int]:
    locs = conn.execute(
        "SELECT source, relpath FROM locations WHERE sha256=? ORDER BY source, relpath LIMIT ?",
        (sha, max_locations)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM locations WHERE sha256=?", (sha,)).fetchone()[0]
    return [{"source": s, "relpath": r} for (s, r) in locs], total


# Substrings marking a real index problem (corrupt / missing table / locked) vs a benign FTS5
# syntax edge. Corruption is surfaced (raised → reindex warning), not hidden behind empty results.
_FTS_CORRUPTION = ("no such table", "no such column", "malformed", "database is locked")


def _fts_rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list:
    """Run an FTS query. Returns [] on a benign FTS5-syntax OperationalError (rare, given
    _sanitize_fts), but RE-RAISES when the error signals a corrupt/missing/locked index so the
    caller turns it into a reindex warning instead of a silent empty result."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if any(s in str(e).lower() for s in _FTS_CORRUPTION):
            raise
        return []


def _make_hit(conn: sqlite3.Connection, sha: str, kind: str, val: str, title: str | None,
              snippet: str, fuzzy: bool, max_locations: int) -> dict:
    """One search-result row — a single keyset for both doc-level and body hits, so the two
    construction sites can't drift apart. `locator_value` is "" for doc-level (title/heading)
    hits: they point at the document, not a page/line."""
    locs, total = _locations_for(conn, sha, max_locations)
    return {"sha256": sha, "locator_kind": kind, "locator_value": val, "title": title or "",
            "fuzzy": fuzzy, "snippet": (snippet or "")[:_SNIPPET_CAP],
            "locations": locs, "total_locations": total}


def search_index(conn: sqlite3.Connection, query: str, source: str | None = None,
                 limit: int = 20, max_locations: int = MAX_LOCATIONS) -> list[dict]:
    """Hits keyed by content sha, one per document. Doc-level signal first: a document whose
    title/headings match ranks above documents that merely mention the terms in body (the Tier 1
    win); body matches follow, AND-first then OR/BM25 fallback. `fuzzy` is per *call*, not per
    hit — True on every body hit when the OR fallback was used, always False on title/heading
    hits. Every hit carries the document `title`. Raises sqlite3.OperationalError on a corrupt
    index (the caller turns that into a reindex warning)."""
    q = query.strip()
    if not q:
        return []
    sanitized = _sanitize_fts(q)
    qtokens = [t.lower() for t in sanitized.split()]              # all tokens, for classification
    qterms = [t for t in qtokens if len(t) > 1]                  # >1-char only — snippet scoring
    src_clause = " AND sha256 IN (SELECT sha256 FROM locations WHERE source=?)"
    src_params: tuple = (source,) if source is not None else ()
    hits: list[dict] = []
    seen: set[str] = set()

    # 1. Doc-level signal — title/heading matches rank first (title weighted over headings).
    doc_sql = "SELECT sha256, title, headings FROM doc_fts WHERE doc_fts MATCH ?"
    if source is not None:
        doc_sql += src_clause
    for sha, title, headings in _fts_rows(
            conn, doc_sql + " ORDER BY bm25(doc_fts, 1.0, 10.0, 5.0) LIMIT ?",
            (sanitized, *src_params, limit)):
        title_tokens = {t.lower() for t in re.findall(r"\w+", title or "")}   # token match, as FTS5
        in_title = any(t in title_tokens for t in qtokens)
        snippet = title if in_title else _best_snippet(headings, qterms)
        hits.append(_make_hit(conn, sha, "title" if in_title else "heading", "",
                              title, snippet, False, max_locations))
        seen.add(sha)

    # 2. Body matches. AND first — GROUP BY sha so a multi-chunk doc yields one row and doesn't eat
    #    the LIMIT window (distinct-doc fetch). OR/BM25 fallback can't GROUP BY (bm25 is per-row),
    #    so it over-fetches ranked rows and dedups to distinct docs in Python.
    base = "SELECT sha256, locator_kind, locator_value, text FROM content_fts WHERE text MATCH ?"
    if source is not None:
        base += src_clause
    headroom = limit + len(seen)                                 # seen docs may also match in body
    brows = _fts_rows(conn, base + " GROUP BY sha256 LIMIT ?", (sanitized, *src_params, headroom))
    fuzzy = False
    terms = [t for t in sanitized.split() if len(t) >= 3] or sanitized.split()
    if not brows and len(terms) > 1:
        ranked = _fts_rows(conn, base + " ORDER BY bm25(content_fts) LIMIT ?",
                           (" OR ".join(terms), *src_params, headroom * 8))
        brows, picked = [], set()
        for row in ranked:
            if row[0] not in picked:
                picked.add(row[0])
                brows.append(row)
                if len(brows) >= headroom:
                    break
        fuzzy = True
    for sha, kind, val, text in brows:
        if sha in seen:                                          # already a title/heading hit
            continue
        hits.append(_make_hit(conn, sha, kind, val, _doc_title(conn, sha),
                              _best_snippet(text, qterms), fuzzy, max_locations))
        seen.add(sha)
    return hits[:limit]


def read_document_text(src: Source, relpath: str, start: int = 1, end: int | None = None) -> str:
    """Read a page range (pdf) or line range (text) from a file in a source.

    A path-less git_repo source reads from its managed clone
    (~/.rtfm/repos/<name>/); a path-less dir source has nothing to read."""
    if src.path is not None:
        root = src.path
    elif src.type == "git_repo":
        root = managed_repo_path(src.name)
        if not root.exists():
            return (f"!!! ERROR !!! source '{src.name}' has no clone yet — "
                    f"run reindex('{src.name}') first.")
    else:
        return f"!!! ERROR !!! source '{src.name}' has no local path."
    path = (root / relpath).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return f"!!! ERROR !!! '{relpath}' escapes source '{src.name}'."
    if not path.exists():
        return f"!!! ERROR !!! '{relpath}' not found in source '{src.name}'."
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path, start=start, end=end)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as dec_err:
        return (f"!!! CANNOT READ '{relpath}' !!! not valid UTF-8 ({dec_err.reason} at byte "
                f"{dec_err.start}). rtfm indexes UTF-8 text. Recover: convert the file, or "
                f"exclude its type in {manifest_path()}.")
    e = end if end is not None else len(lines)
    return "\n".join(lines[max(0, start - 1):e])


mcp = FastMCP("rtfm")


@mcp.tool()
def search(query: str, source: str | None = None, max_files: int = 20,
           max_locations: int = MAX_LOCATIONS) -> dict:
    """Search the corpus. Returns dedup'd hits, each listing up to `max_locations` paths its
    content lives at, plus total_locations.

    Auto-reindex: before searching, every dir source touched by this query is checked for
    staleness with a cheap (relpath, mtime) scan — no extraction. A source within the
    auto-reindex budget (RTFM_AUTO_REINDEX_MAX, default 10 new/changed files) is reindexed
    inline so newly-added/edited files just work; a larger delta is left to an explicit
    reindex() and reported in WARNING rather than blocking the query on extraction.
    git_repo sources always auto-reindex when stale (a cheap commit comparison — after
    fetch for managed clones, against the tree's HEAD for linked) — the budget doesn't
    apply; a failed refresh is reported in WARNING and previously indexed content is
    searched. This refreshes the search cache only — it never mutates user-owned files;
    managed clones are rtfm's own and are refreshed by design.

    Args:
        query: text to search for.
        source: restrict to one source name (None = all).
        max_files: cap on returned hits.
        max_locations: cap on paths listed per hit (use find_duplicates for the full list).
    """
    sources, warnings = load_manifest()
    warnings = list(warnings)
    conn = get_index_db()
    budget = _auto_reindex_max()
    warned_repo_paths: set[str] = set()      # one warning per clone, not per source
    for s in sources:
        if source is not None and s.name != source:
            continue
        if s.type not in ("dir", "git_repo"):
            continue
        try:                                         # one source's refresh never fails the query
            verdict = _freshness(conn, s)
            if s.type == "dir":
                result = None
                if verdict.stale and verdict.changed <= budget:
                    # Re-uses the freshness handler's walk: one traversal per query.
                    result = _ingest(conn, s, s.path, verdict.scan)
                elif verdict.stale:
                    warnings.append(
                        f"!!! STALE SOURCE '{s.name}' !!! {verdict.changed} new/changed files "
                        f"exceed the auto-reindex budget ({budget}) — searching previously "
                        f"indexed content only. Recover: run reindex('{s.name}').")
                if result is None and verdict.scan is not None:
                    # Fresh, or too big to index inline: steps 1 and 2 still have something to
                    # say and the scan is in hand. An empty 3A/3B tail keeps `report_all` the
                    # one reporting path (ADR 0015).
                    result = _EMPTY_INDEXED._replace(reachable=verdict.scan.reachable,
                                                     wanted=verdict.scan.wanted)
                if result is not None:
                    warnings.extend(report_all(conn, s, result))
                continue
            # Not reindexing this query: no scan to report from, but the store answers free.
            if not verdict.stale:
                warnings.extend(report_unsearchable(conn, s.name))
                continue
            if verdict.cached:
                # The verdict came from the memo (<= STALENESS_TTL old): the reindex
                # attempt and its failure warning run once per window, not on every
                # query — a persistently broken or dirty source must not block or
                # spam every search (ADR 0013).
                warnings.extend(report_unsearchable(conn, s.name))
                continue
            # git_repo — always auto-reindex, budget doesn't apply
            summary = reindex_source(conn, s)
            warnings.extend(summary.get("warnings", ()))
            # Multiple sources on one clone (or one broken remote) would
            # otherwise repeat the identical warning once per source.
            key = (s.path if s.path is not None
                   else str(managed_repo_path(s.name)))
            message = None
            if summary.get("error"):
                message = (f"!!! AUTO-REINDEX FAILED '{s.name}' !!! {summary['error']} — "
                           f"searching previously indexed content only. "
                           f"Recover: run reindex('{s.name}').")
            elif summary.get("warning"):
                # A non-blocking warning (e.g. a linked ref that doesn't
                # resolve) — surface it when a refresh does run.
                message = f"!!! SOURCE WARNING '{s.name}' !!! {summary['warning']}"
            if message and key not in warned_repo_paths:
                warned_repo_paths.add(key)
                warnings.append(message)
        except Exception as e:
            warnings.append(
                f"!!! AUTO-REINDEX FAILED '{s.name}' !!! {type(e).__name__}: {e} — searching "
                f"previously indexed content only. Recover: run reindex('{s.name}').")
    try:
        hits = search_index(conn, query, source=source, limit=max_files,
                            max_locations=max_locations)
    except sqlite3.Error as e:                           # corrupt/missing/locked index
        hits = []
        warnings.append(
            f"!!! INDEX ERROR !!! {e} — the search index looks corrupt or busy. "
            f"Recover: run reindex() to rebuild it.")
    resp: dict = {"results": hits,
                  "sources_searched": [s.name for s in sources if s.type in ("dir", "git_repo")
                                       and (source is None or s.name == source)]}
    if any(h.get("fuzzy") for h in hits):                # OR/BM25 fallback — terms didn't co-occur
        resp["fuzzy"] = True
    if warnings:
        resp["WARNING"] = warnings
    if not query.strip():
        resp["error"] = "Query must be non-empty."
    return resp


@mcp.tool()
def reindex(source: str | None = None) -> dict:
    """Build/refresh the index. The ONLY tool that extracts. Pass a source name to rebuild
    just that source, or omit to rebuild all dir and git_repo sources. Returns a per-source
    summary."""
    sources, warnings = load_manifest()
    conn = get_index_db()
    targets = [s for s in sources if s.type in ("dir", "git_repo")
               and (source is None or s.name == source)]
    if source is not None and not targets:
        return {"error": f"source '{source}' not found. Call list_sources()."}
    # Rebuild semantics: sources removed from the manifest (or dropped at load, e.g.
    # url-less git_repo) must not keep serving their leftover index rows. Guarded on
    # a non-empty manifest: with none (unparseable, or all sources commented out)
    # the purge must NOT run — `x NOT IN ()` is true for every row in SQLite, so an
    # unguarded purge wipes the whole index exactly when the user is mid-edit.
    manifest_names = {s.name for s in sources}
    dropped: list[str] = []
    if manifest_names:
        dropped = [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM locations WHERE source NOT IN "
            f"({','.join('?' * len(manifest_names))})", tuple(manifest_names))]
        for name in dropped:
            conn.execute("DELETE FROM locations WHERE source=?", (name,))
            conn.execute("DELETE FROM source_meta WHERE source=?", (name,))
            for key in [k for k in _freshness_cache if k[0] == name]:
                _freshness_cache.pop(key, None)
        if dropped:
            conn.commit()
    resp: dict = {"reindexed": [reindex_source(conn, s) for s in targets]}
    for summary in resp["reindexed"]:
        warnings.extend(summary.pop("warnings", ()))
    if dropped:
        resp["purged_sources"] = dropped
    if warnings:
        resp["WARNING"] = warnings
    return resp


@mcp.tool()
def find_duplicates(source: str | None = None, min_locations: int = 2) -> dict:
    """List contents stored at multiple paths (byte-identical files under different paths or
    versions). With `source`, reports contents duplicated WITHIN that source — the count and
    listed paths are scoped to it. Without `source`, reports duplicates across the whole
    corpus. Groups are sorted by location count, descending."""
    conn = get_index_db()
    if source is not None:
        rows = conn.execute(
            "SELECT sha256, COUNT(*) c FROM locations WHERE source=? "
            "GROUP BY sha256 HAVING c >= ?", (source, min_locations)).fetchall()
    else:
        rows = conn.execute(
            "SELECT sha256, COUNT(*) c FROM locations GROUP BY sha256 HAVING c >= ?",
            (min_locations,)).fetchall()
    groups = []
    for sha, c in rows:
        if source is not None:
            locs = conn.execute(
                "SELECT source, relpath FROM locations WHERE sha256=? AND source=? "
                "ORDER BY source, relpath", (sha, source)).fetchall()
        else:
            locs = conn.execute(
                "SELECT source, relpath FROM locations WHERE sha256=? "
                "ORDER BY source, relpath", (sha,)).fetchall()
        kind = conn.execute("SELECT locator_kind FROM contents WHERE sha256=?", (sha,)).fetchone()
        groups.append({"sha256": sha, "n_locations": c,
                       "locator_kind": kind[0] if kind else None,
                       "locations": [{"source": s, "relpath": r} for (s, r) in locs]})
    groups.sort(key=lambda g: g["n_locations"], reverse=True)
    return {"duplicates": groups}


@mcp.tool()
def read(source: str, relpath: str, start: int = 1, end: int | None = None) -> str:
    """Read a page range (PDF) or line range (text) from a file in a source.

    Use after `search` to read around a hit's locator.
    """
    sources, _ = load_manifest()
    match = next((s for s in sources if s.name == source), None)
    if match is None:
        return (f"!!! ERROR !!! source '{source}' not found. "
                f"Recover: call list_sources().")
    return read_document_text(match, relpath, start, end)


@mcp.tool()
def list_sources() -> dict:
    """List configured sources with indexed-file and unique-content counts. Read-only:
    never mutates the index; git_repo status for managed sources may fetch.

    git_repo sources also report their url, the ref being tracked (declared ref or the
    remote's default branch), and a git_status in git's own terms: "up to date" (indexed
    commit == origin/ref), "behind" (origin/ref moved on), "ahead" (the indexed content
    is ahead of origin/ref — a linked clone with unpushed commits), "diverged" (both
    sides moved), "dirty" (uncommitted changes in the working tree), "detached"
    (tracking a pinned SHA — git's detached-HEAD state; staleness undefined), plus rtfm's
    operational states: "never indexed" (no source_meta row yet), "unknown" (the ref
    doesn't resolve — check the ref spelling in the manifest; for managed sources also
    a failed fetch — check the network), or an "error: ..." string (a git call failed;
    the detail names it). Linked clones are read-only: the comparison uses the clone's
    own local refs, never a fetch. One bad source never breaks the rest: git failures
    degrade to a status string, never an exception.
    """
    sources, warnings = load_manifest()
    conn = get_index_db()
    out = []
    for s in sources:
        item: dict = {"name": s.name, "type": s.type,
                      "path": str(s.path) if s.path else str(managed_repo_path(s.name)),
                      "mutable": s.mutable,
                      "indexed_files": conn.execute(
                          "SELECT COUNT(*) FROM locations WHERE source=?",
                          (s.name,)).fetchone()[0],
                      "unique_contents": conn.execute(
                          "SELECT COUNT(DISTINCT sha256) FROM locations WHERE source=?",
                          (s.name,)).fetchone()[0]}
        if s.type == "git_repo":
            item["url"] = s.url
            repo_path = s.path if s.path else managed_repo_path(s.name)
            # Guarded: a path-less never-cloned repo has no origin to ask, and one bad
            # source must never break the listing for the others.
            try:
                item["ref"] = s.ref or _default_branch(repo_path)
            except RuntimeError:
                item["ref"] = s.ref

            meta = conn.execute(
                "SELECT git_commit FROM source_meta WHERE source=?",
                (s.name,)).fetchone()
            if meta is None:
                item["git_status"] = "never indexed"
            else:
                try:
                    clean, _ = git_ops.is_clean(repo_path)
                    if not clean:
                        item["git_status"] = "dirty"
                    elif s.path is not None:
                        item["git_status"] = _linked_git_status(repo_path, s, meta[0])
                    else:
                        item["git_status"] = _managed_git_status(repo_path, s, meta[0])
                except Exception as e:
                    item["git_status"] = f"error: {e}"
        out.append(item)
    resp = {"sources": out}
    if warnings:
        resp["WARNING"] = warnings
    return resp


@mcp.tool()
def health_check() -> dict:
    """Check server health: corpus home, schema version, index DB, PDF extractors,
    git presence, sources."""
    status: dict = {"server": "rtfm", "ok": True, "issues": []}
    status["corpus_home"] = str(corpus_home())
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True, timeout=5)
        status["pdftotext"] = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        status["pdftotext"] = False
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        status["git"] = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # A git-less machine is healthy-looking until every git_repo op fails —
        # the probe keeps the health check truthful for managed-only sources.
        status["git"] = False
    for mod in ("fitz", "pypdf"):
        try:
            __import__(mod)
            status[mod] = True
        except ImportError:
            status[mod] = False
    try:
        conn = get_index_db()
        status["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        sources, warnings = load_manifest()
        status["sources"] = [{"name": s.name, "type": s.type,
                              **( {"url": s.url, "ref": s.ref} if s.type == "git_repo" else {})}
                             for s in sources]
        for src in sources:
            if src.type not in ("dir", "git_repo"):
                continue
            root = src.path if src.path is not None else managed_repo_path(src.name)
            if not root.exists():
                continue
            reachable = access(src, root)
            issues = report(src.name, reachable, "access")
            if issues:
                status["ok"] = False
                status["issues"].extend(issues)
        # Asking per manifest source also skips rows left behind by a source since deleted
        # from the manifest, with no name filtering of its own.
        for src in sources:
            issues = report_unsearchable(conn, src.name)
            if issues:
                status["ok"] = False
                status["issues"].extend(issues)
        if not status.get("git") and any(s.type == "git_repo" for s in sources):
            # git_repo sources cannot be refreshed without git — a green 'ok' with
            # an impossible corpus is a silent failure.
            status["ok"] = False
            status["issues"].append(
                "git executable not found — git_repo sources cannot be refreshed. "
                "Recover: install git, or remove the git_repo sources.")
        if warnings:
            status["issues"].extend(warnings)
            status["ok"] = False
    except Exception as e:
        status["ok"] = False
        status["issues"].append(f"index/manifest error: {e}")
    return status


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
