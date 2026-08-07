#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]",
#   "pymupdf",
#   "pypdf",
# ]
# ///
"""rtfm — Read The Full Manual. Local document-corpus search MCP server."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP

# --- config -----------------------------------------------------------------
_log = logging.getLogger("rtfm")

TEXT_EXTS = {".txt", ".md", ".rst", ".rest"}   # plain text → line locators (.html later)
CHUNK_LINES = 50
SCHEMA_VERSION = 4                   # index DB is a cache; mismatch ⇒ drop & rebuild
MAX_LOCATIONS = 5                    # default cap on locations listed per search hit


def corpus_home() -> Path:
    return Path(os.environ.get("RTFM_HOME", Path.home() / ".rtfm")).expanduser()


def manifest_path() -> Path:
    return corpus_home() / "manifest.toml"


def default_source_dir() -> Path:
    return corpus_home() / "default"


def index_db_path() -> Path:
    return corpus_home() / "cache" / "index.db"


def _managed_repo_path(name: str) -> Path:
    """Path rtfm uses when a git_repo source omits `path`."""
    return corpus_home() / "repos" / name


def _git_timeout() -> int:
    """Timeout in seconds for git subprocess calls. 0 = no timeout."""
    env = os.environ.get("RTFM_GIT_TIMEOUT")
    if env and env.isdigit():
        return int(env)
    return 60

# --- git operations ----------------------------------------------------------

def _git(args: list[str], cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a git command. Raises RuntimeError on non-zero exit or timeout."""
    t = timeout if timeout is not None else _git_timeout()
    try:
        cp = subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
            timeout=t if t > 0 else None,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} timed out after {t}s") from None
    if cp.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} exited {cp.returncode}: {(cp.stderr or '').strip()[:200]}")
    return cp


def _git_repo_root(path: Path) -> Path | None:
    """Return the git repo root, or None if `path` is not in a git repo."""
    try:
        cp = _git(["rev-parse", "--show-toplevel"], cwd=path, timeout=10)
        return Path(cp.stdout.strip())
    except RuntimeError:
        return None


def _git_remote_url(path: Path) -> str:
    """Return the origin remote URL, or '' if no origin."""
    try:
        cp = _git(["remote", "get-url", "origin"], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError:
        return ""


def _git_is_clean(path: Path) -> tuple[bool, list[str]]:
    """(is_clean, list_of_dirty_files). A clean tree has no modified or untracked files."""
    cp = _git(["status", "--porcelain"], cwd=path, timeout=10)
    if not cp.stdout.strip():
        return True, []
    # Parse dirty file paths: `git status --porcelain` gives "XY filename". splitlines(),
    # not strip+split: an unstaged edit is " M file" and strip() eats the leading status
    # space, shifting the path one column left.
    dirty = [line[3:].strip() for line in cp.stdout.splitlines() if len(line) >= 4]
    return False, dirty


def _git_current_commit(path: Path) -> str:
    """Return the SHA of HEAD."""
    cp = _git(["rev-parse", "HEAD"], cwd=path, timeout=10)
    return cp.stdout.strip()


def _git_commit_date(path: Path, ref: str) -> str:
    """Return ISO 8601 commit date for `ref`."""
    cp = _git(["log", "-1", "--format=%cI", ref], cwd=path, timeout=10)
    return cp.stdout.strip()


def _git_resolve_ref(path: Path, ref: str) -> str:
    """Resolve a ref to a commit SHA. For branches, tries origin/<ref> first.
    For tags and SHAs, resolves directly. Raises RuntimeError if ref doesn't exist."""
    # Branches: origin/<ref> first — after a fetch, origin/<ref> is the remote's current
    # commit while the local branch may be behind, so local-first would hide remote updates.
    try:
        cp = _git(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError:
        pass
    # Fall back to local (works for SHAs, tags, local-only branches, HEAD)
    try:
        cp = _git(["rev-parse", "--verify", ref], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError:
        raise RuntimeError(
            f"ref '{ref}' not found as origin/{ref} or locally in {path}. "
            f"Recover: verify the ref name, or run 'git fetch' in the repo.") from None


def _git_fetch(path: Path, timeout: int | None = None) -> None:
    """git fetch origin. Timeout-guarded; raises RuntimeError on failure."""
    t = timeout if timeout is not None else _git_timeout()
    _git(["fetch", "origin"], cwd=path, timeout=t)


def _git_clone(url: str, ref: str | None, dest: Path, timeout: int | None = None) -> None:
    """Clone a repo. Full clone (no --depth). With a ref, clones and checks out that
    branch/tag/SHA; with ref=None, checks out the remote's default branch (git rejects
    `--branch HEAD`, so a ref-less clone omits --branch entirely). Raises RuntimeError."""
    t = timeout if timeout is not None else _git_timeout()
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", url, str(dest)]
    if ref:
        args[1:1] = ["--branch", ref]
    _git(args, cwd=dest.parent, timeout=t)


def _git_checkout(path: Path, ref: str) -> None:
    """Check out `ref`. For branches: reset to match origin/<ref>. For tags/SHAs: direct checkout.
    Raises RuntimeError on failure."""
    # Determine if `ref` is a branch by checking if origin/<ref> exists
    is_branch = False
    try:
        _git(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        is_branch = True
    except RuntimeError:
        pass

    if is_branch:
        # Fetch then reset to match remote
        _git_fetch(path)
        _git(["checkout", "-B", ref, f"origin/{ref}"], cwd=path,
             timeout=_git_timeout())
    else:
        # Tag or SHA: direct checkout (may need fetch first for tags)
        try:
            _git(["checkout", ref], cwd=path, timeout=_git_timeout())
        except RuntimeError:
            # Maybe it's a tag we haven't fetched; try fetch then checkout
            _git_fetch(path)
            _git(["checkout", ref], cwd=path, timeout=_git_timeout())

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
            git_commit_date TEXT NOT NULL
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


def _rows_for_file(path: Path) -> list[tuple[str, str, str]]:
    """Return (locator_kind, locator_value, text) rows for a supported file, else []."""
    ext = path.suffix.lower()
    rows: list[tuple[str, str, str]] = []
    if ext == ".pdf":
        text = extract_pdf_text(path)
        for page_num, page_text in enumerate(text.split("\f"), 1):
            content = "\n".join(ln.strip() for ln in page_text.splitlines() if ln.strip())
            if content:
                rows.append(("page", str(page_num), content))
    elif ext in TEXT_EXTS:
        lines = path.read_text(errors="replace").splitlines()
        for i in range(0, max(1, len(lines)), CHUNK_LINES):
            chunk = "\n".join(ln.rstrip() for ln in lines[i:i + CHUNK_LINES])
            if chunk.strip():
                rows.append(("line", str(i + 1), chunk))
    return rows


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
    lines = path.read_text(errors="replace").splitlines()
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


def _doc_signal_for_file(path: Path) -> tuple[str, str]:
    """Doc-level (title, headings) for ranking. Best-effort — a failure yields ("", "") so the
    document still ranks on body text and signal extraction never blocks body extraction. The
    failure is logged (not silent): a broken/absent pymupdf would otherwise strip title/heading
    ranking corpus-wide with nothing to grep."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _pdf_doc_signal(path)
        if ext in TEXT_EXTS:
            return _text_doc_signal(path)
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


def _ensure_git_repo_ready(src: Source) -> tuple[Path, str | None]:
    """Ensure a git_repo source has a clone at the right commit. Returns (repo_path, error).
    On success, error is None. On failure, error is a classified message."""
    # Determine repo path
    if src.path is not None:
        repo_path = src.path
        # Linked mode: verify it's a git repo with matching remote (already validated
        # in load_manifest, but re-verify in case state changed)
        root = _git_repo_root(repo_path)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} is not a git repository. "
                f"Recover: clone the repo or remove 'path' from the manifest.")
        remote = _git_remote_url(root)
        if remote and remote != src.url:
            return repo_path, (
                f"ERROR:REMOTE_MISMATCH: origin is '{remote}', manifest declares "
                f"'{src.url}'. Recover: fix the manifest or update the clone's origin.")
    else:
        # Managed mode: clone if needed
        repo_path = _managed_repo_path(src.name)
        if not (repo_path / ".git").exists():
            try:
                _git_clone(src.url, src.ref, repo_path)   # ref None → default branch
            except RuntimeError as e:
                return repo_path, (
                    f"ERROR:CLONE_FAILED: {e}. "
                    f"Recover: verify the URL and network, or provide a 'path' to an "
                    f"existing clone.")
        else:
            # Verify existing managed clone
            remote = _git_remote_url(repo_path)
            if remote and remote != src.url:
                return repo_path, (
                    f"ERROR:REMOTE_MISMATCH: managed clone at {repo_path} has origin "
                    f"'{remote}', manifest declares '{src.url}'. "
                    f"Recover: remove {repo_path} and reindex, or fix the manifest.")
            if _git_repo_root(repo_path) is None:
                return repo_path, (
                    f"ERROR:NOT_GIT_REPO: {repo_path} exists but is not a git repository. "
                    f"Recover: remove {repo_path} and reindex.")

    # Ref to use
    try:
        ref = src.ref or _default_branch(repo_path)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:NO_REMOTE: can't determine default branch: {e}. "
            f"Recover: set 'ref' in the manifest or add an 'origin' remote to {repo_path}.")

    # Fetch and checkout
    try:
        _git_fetch(repo_path)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:FETCH_FAILED: {e}. "
            f"Recover: check network connectivity and try again.")

    try:
        _git_checkout(repo_path, ref)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:CHECKOUT_FAILED: ref '{ref}' — {e}. "
            f"Recover: verify the ref exists on the remote and try again.")

    # Dirty check
    clean, dirty_files = _git_is_clean(repo_path)
    if not clean:
        return repo_path, (
            f"ERROR:DIRTY_TREE: {len(dirty_files)} file(s) modified or untracked — "
            f"{', '.join(dirty_files[:10])}. "
            f"Recover: commit, stash, or clean the working tree, then reindex.")

    return repo_path, None


def _reindex_git_repo(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild a git_repo source."""
    repo_path, error = _ensure_git_repo_ready(src)
    if error:
        return {"source": src.name, "error": error}

    # Record commit before indexing
    commit = _git_current_commit(repo_path)
    commit_date = _git_commit_date(repo_path, "HEAD")
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date) VALUES(?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "git_commit=excluded.git_commit, git_commit_date=excluded.git_commit_date",
        (src.name, commit, commit_date))
    conn.commit()

    # Now run the file-level indexing (same logic as dir, but from repo_path)
    summary: dict = {"source": src.name, "files_seen": 0, "unique_contents": 0,
                     "newly_extracted": 0, "extraction_skips": 0, "purged": 0,
                     "errors": 0, "commit": commit, "commit_date": commit_date}

    files = iter_source_files(Source(name=src.name, type="dir", path=repo_path))
    summary["files_seen"] = len(files)

    existing = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT relpath, sha256, mtime FROM locations WHERE source=?", (src.name,))}

    present: dict[str, tuple[str, float]] = {}
    for f in files:
        rel = str(f.relative_to(repo_path))
        mtime = f.stat().st_mtime
        prev = existing.get(rel)
        if prev and prev[1] == mtime:
            sha = prev[0]
        else:
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
        present[rel] = (sha, mtime)

    vanished = set(existing) - set(present)
    for rel in vanished:
        conn.execute("DELETE FROM locations WHERE source=? AND relpath=?", (src.name, rel))
    summary["purged"] = len(vanished)

    for rel, (sha, mtime) in present.items():
        conn.execute(
            "INSERT INTO locations(source, relpath, sha256, mtime) VALUES(?,?,?,?) "
            "ON CONFLICT(source, relpath) DO UPDATE SET "
            "sha256=excluded.sha256, mtime=excluded.mtime",
            (src.name, rel, sha, mtime))
    conn.commit()

    shas_here = {sha for (sha, _) in present.values()}
    summary["unique_contents"] = len(shas_here)
    already = {r[0] for r in conn.execute("SELECT sha256 FROM contents")}
    need = shas_here - already
    summary["extraction_skips"] = summary["files_seen"] - len(need)

    jobs: dict[str, str] = {}
    for rel in sorted(present):
        sha = present[rel][0]
        if sha in need and sha not in jobs:
            jobs[sha] = str(repo_path / rel)

    for ex in _extract_many(list(jobs.items())):
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
        summary["errors" if ex.error else "newly_extracted"] += 1
    conn.commit()

    for tbl in ("content_fts", "doc_fts", "contents"):
        conn.execute(
            f"DELETE FROM {tbl} WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM locations)")
    conn.commit()
    return summary


def reindex_source(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild one source. Dispatches on type: git_repo sources go through the
    fetch/checkout/dirty-check flow; dir sources index in place (unchanged). Returns a summary.

    extraction_skips = files_seen - unique contents that required fresh extraction this run.
    Covers two cases: byte-identical duplicates within the same run (same sha, only one job
    submitted) and contents already extracted in a prior run (cache hits in `contents`).
    Value: files_seen - len(need), where need = unique shas not yet in contents.
    """
    if src.type == "git_repo":
        return _reindex_git_repo(conn, src)
    summary = {"source": src.name, "files_seen": 0, "unique_contents": 0,
               "newly_extracted": 0, "extraction_skips": 0, "purged": 0, "errors": 0}
    if src.path is None or not src.path.exists():
        return summary
    files = iter_source_files(src)
    summary["files_seen"] = len(files)

    existing = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT relpath, sha256, mtime FROM locations WHERE source=?", (src.name,))}

    present: dict[str, tuple[str, float]] = {}
    for f in files:
        rel = str(f.relative_to(src.path))
        mtime = f.stat().st_mtime
        prev = existing.get(rel)
        if prev and prev[1] == mtime:
            sha = prev[0]                                   # mtime match: reuse stored sha
        else:
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
        present[rel] = (sha, mtime)

    vanished = set(existing) - set(present)
    for rel in vanished:
        conn.execute("DELETE FROM locations WHERE source=? AND relpath=?", (src.name, rel))
    summary["purged"] = len(vanished)

    for rel, (sha, mtime) in present.items():
        conn.execute(
            "INSERT INTO locations(source, relpath, sha256, mtime) VALUES(?,?,?,?) "
            "ON CONFLICT(source, relpath) DO UPDATE SET "
            "sha256=excluded.sha256, mtime=excluded.mtime",
            (src.name, rel, sha, mtime))
    conn.commit()

    shas_here = {sha for (sha, _) in present.values()}
    summary["unique_contents"] = len(shas_here)
    already = {r[0] for r in conn.execute("SELECT sha256 FROM contents")}
    need = shas_here - already
    summary["extraction_skips"] = summary["files_seen"] - len(need)

    jobs: dict[str, str] = {}
    for rel in sorted(present):                             # deterministic representative path
        sha = present[rel][0]
        if sha in need and sha not in jobs:
            jobs[sha] = str(src.path / rel)

    for ex in _extract_many(list(jobs.items())):
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
        summary["errors" if ex.error else "newly_extracted"] += 1
    conn.commit()

    for tbl in ("content_fts", "doc_fts", "contents"):           # GC contents with no live path
        conn.execute(
            f"DELETE FROM {tbl} WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM locations)")
    conn.commit()
    return summary


def _stale_delta(conn: sqlite3.Connection, src: Source) -> tuple[int, bool]:
    """Cheap staleness check. Dispatches on source type.
    Returns (changed, stale):
      changed  count of new/changed items (files for dir, always 0 for git_repo)
      stale    whether the index differs from reality
    """
    if src.type == "git_repo":
        return _stale_delta_git_repo(conn, src)
    # dir sources: stat + (relpath, mtime) compare — no hashing or extraction.
    # `changed` counts files that would need fresh extraction and bounds the cost of an
    # inline auto-reindex (see _auto_reindex_max); `stale` also catches files that vanished
    # on disk (a reindex purges them essentially for free, so they don't inflate `changed`).
    if src.path is None or not src.path.exists():
        return 0, False
    indexed = {r[0]: r[1] for r in conn.execute(
        "SELECT relpath, mtime FROM locations WHERE source=?", (src.name,))}
    on_disk = {str(f.relative_to(src.path)): f.stat().st_mtime for f in iter_source_files(src)}
    changed = sum(1 for rel, mtime in on_disk.items() if indexed.get(rel) != mtime)
    stale = changed > 0 or set(indexed) != set(on_disk)   # latter catches vanished files
    return changed, stale


def _stale_delta_git_repo(conn: sqlite3.Connection, src: Source) -> tuple[int, bool]:
    """Commit-based staleness for git_repo: compare the indexed commit to origin/<ref>.
    Fetches first so the remote state is current. Returns (0, stale)."""
    try:
        row = conn.execute(
            "SELECT git_commit FROM source_meta WHERE source=?", (src.name,)).fetchone()
        if row is None:
            return 0, True  # never indexed — always stale
        repo_path = src.path if src.path is not None else _managed_repo_path(src.name)
        if not repo_path.exists():
            return 0, True  # clone vanished
        _git_fetch(repo_path)
        ref = src.ref or _default_branch(repo_path)
        current = _git_resolve_ref(repo_path, ref)
    except Exception:
        return 0, True  # graceful degradation: any failure means stale (will warn)
    return 0, (row[0] != current)


def _default_branch(path: Path) -> str:
    """Return the remote's default branch name (origin's HEAD branch), 'main' as fallback."""
    cp = _git(["remote", "show", "origin"], cwd=path, timeout=_git_timeout())
    for line in cp.stdout.splitlines():
        if "HEAD branch:" in line:
            return line.split("HEAD branch:")[1].strip()
    return "main"  # sensible fallback


def iter_source_files(src: Source) -> list[Path]:
    """Supported files under a dir source, recursively, skipping hidden dirs."""
    if src.path is None or not src.path.exists():
        return []
    out = []
    for f in src.path.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() != ".pdf" and f.suffix.lower() not in TEXT_EXTS:
            continue
        if any(part.startswith(".") for part in f.relative_to(src.path).parts):
            continue
        out.append(f)
    return sorted(out)

# --- manifest ---------------------------------------------------------------

@dataclass
class Source:
    name: str
    type: str                       # "dir" or "git_repo"
    path: Path | None = None
    url: str | None = None
    ref: str | None = None          # git refspec (branch, tag, SHA); None for dir
    mutable: bool = False


_BOOTSTRAP_MANIFEST = '''\
# rtfm source manifest. See manifest.example.toml in the repo for all options.
# Each [[source]] is one place rtfm indexes, in place.

[[source]]
name    = "default"   # the zero-config drop-dir; the only mutable source by default
type    = "dir"
path    = "{default}"
mutable = true

# Example: a git-tracked doc repo. rtfm clones and tracks the ref automatically
# when `path` is omitted (managed mode), or links to an existing clone when
# `path` is provided. `ref` defaults to the remote's HEAD branch.
# [[source]]
# name    = "specs"
# type    = "git_repo"
# url     = "https://github.com/org/specs.git"
# ref     = "main"
'''


def _ensure_bootstrap() -> None:
    home = corpus_home()
    home.mkdir(parents=True, exist_ok=True)
    default_source_dir().mkdir(parents=True, exist_ok=True)
    index_db_path().parent.mkdir(parents=True, exist_ok=True)
    mp = manifest_path()
    if not mp.exists():
        mp.write_text(_BOOTSTRAP_MANIFEST.format(default=default_source_dir()))


def _source_from_table(t: dict) -> Source:
    path = t.get("path")
    url = t.get("url")
    name = t.get("name")
    if not name:
        basis = path or url or "source"
        name = Path(str(basis)).name or "source"
    return Source(
        name=name,
        type=t.get("type", "dir"),
        path=Path(path).expanduser() if path else None,
        url=url,
        ref=t.get("ref"),                        # None if absent → default to remote HEAD later
        mutable=bool(t.get("mutable", False)),
    )


def _validate_source(s: Source) -> str | None:
    """Loud warning if a source is misconfigured, else None. A dir source needs an existing,
    readable directory; a `path`-less dir source is unusable (the caller drops it). A git_repo
    source needs a url; in linked mode (`path` set) the path must be a git working tree whose
    origin remote matches the declared url. The point is that one bad entry never silently
    disappears and never breaks the others."""
    if s.type == "git_repo":
        if not s.url:
            return (f"!!! INVALID SOURCE '{s.name}' !!! git_repo source has no 'url' — "
                    f"a remote URL is required. Recover: add url = \"...\" in "
                    f"{manifest_path()}.")
        if s.path is not None:
            # Linked mode: verify the path exists and is a git repo with a matching remote
            if not s.path.exists():
                return (f"!!! SOURCE PATH MISSING '{s.name}' !!! {s.path} does not exist. "
                        f"Recover: fix 'path' in {manifest_path()} or remove it to let rtfm "
                        f"manage the clone.")
            if not s.path.is_dir():
                return (f"!!! SOURCE PATH NOT A DIRECTORY '{s.name}' !!! {s.path} is a file. "
                        f"Recover: point 'path' at a git working tree.")
            root = _git_repo_root(s.path)
            if root is None:
                return (f"!!! NOT A GIT REPO '{s.name}' !!! {s.path} is not a git repository. "
                        f"Recover: clone the repo to that path, or remove 'path' to let rtfm "
                        f"manage the clone.")
            remote = _git_remote_url(root)
            if not remote:
                return (f"!!! NO REMOTE '{s.name}' !!! {s.path} has no 'origin' remote. "
                        f"Recover: add a remote with 'git remote add origin <url>'.")
            if remote != s.url:
                return (f"!!! REMOTE URL MISMATCH '{s.name}' !!! {s.path} has origin "
                        f"'{remote}', but manifest declares '{s.url}'. "
                        f"Recover: fix 'url' in {manifest_path()} or update the clone's "
                        f"origin with 'git remote set-url origin {s.url}'.")
        return None
    if s.type != "dir":
        return None
    if s.path is None:
        return (f"!!! INVALID SOURCE '{s.name}' !!! dir source has no 'path' — skipping it. "
                f"Recover: add path = \"...\" in {manifest_path()}.")
    if not s.path.exists():
        return (f"!!! SOURCE PATH MISSING '{s.name}' !!! {s.path} does not exist — this source "
                f"indexes nothing. Recover: fix 'path' in {manifest_path()} or remove it.")
    if not s.path.is_dir():
        return (f"!!! SOURCE PATH NOT A DIRECTORY '{s.name}' !!! {s.path} is a file, not a "
                f"directory. Recover: point 'path' at a folder in {manifest_path()}.")
    if not os.access(s.path, os.R_OK):
        return (f"!!! SOURCE UNREADABLE '{s.name}' !!! {s.path} is not readable (permissions). "
                f"Recover: fix permissions or 'path' in {manifest_path()}.")
    return None


def load_manifest() -> tuple[list[Source], list[str]]:
    """Return (sources, warnings). Bootstraps a default manifest if none exists.

    Duplicate names are resolved first-wins; each refused duplicate yields a loud, actionable
    warning (ADR 0006). Misconfigured sources are validated: a `path`-less dir source is dropped,
    a missing/non-dir/unreadable path is kept-but-warned (so it stays visible and self-heals if
    the path appears). Never raises — one bad entry never breaks the rest.
    """
    _ensure_bootstrap()
    try:
        data = tomllib.loads(manifest_path().read_text())
    except tomllib.TOMLDecodeError as e:
        return [], [
            f"!!! MALFORMED MANIFEST !!! {manifest_path()} is not valid TOML: {e}. "
            f"No sources loaded. Recover: fix the TOML syntax in that file "
            f"(see manifest.example.toml for the format)."
        ]
    tables = data.get("source", [])
    sources: list[Source] = []
    warnings: list[str] = []
    seen: dict[str, Source] = {}
    for t in tables:
        s = _source_from_table(t)
        if s.name in seen:
            warnings.append(
                f"!!! DUPLICATE SOURCE NAME '{s.name}' !!! "
                f"keeping {seen[s.name].path or seen[s.name].url}, REFUSING "
                f"{s.path or s.url}. Source names must be unique. "
                f"Recover: rename one in {manifest_path()} "
                f"(version-stamp them, e.g. '{s.name}-2025.06')."
            )
            continue
        warn = _validate_source(s)
        if warn:
            warnings.append(warn)
            if s.type == "dir" and s.path is None:
                continue                       # unusable — drop it (loudly, above)
        seen[s.name] = s
        sources.append(s)
    return sources, warnings
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
    """Read a page range (pdf) or line range (text) from a file in a source."""
    if src.path is None:
        return f"!!! ERROR !!! source '{src.name}' has no local path."
    path = (src.path / relpath).resolve()
    try:
        path.relative_to(src.path.resolve())
    except ValueError:
        return f"!!! ERROR !!! '{relpath}' escapes source '{src.name}'."
    if not path.exists():
        return f"!!! ERROR !!! '{relpath}' not found in source '{src.name}'."
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path, start=start, end=end)
    lines = path.read_text(errors="replace").splitlines()
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
    git_repo sources always auto-reindex when stale (a cheap commit comparison after fetch)
    — the budget doesn't apply; a failed refresh is reported in WARNING and previously
    indexed content is searched. This refreshes the search cache only — it never mutates
    the source files.

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
    for s in sources:
        if source is not None and s.name != source:
            continue
        if s.type not in ("dir", "git_repo"):
            continue
        try:                                         # one source's refresh never fails the query
            changed, stale = _stale_delta(conn, s)
            if not stale:
                continue
            if s.type == "dir":
                if changed <= budget:
                    reindex_source(conn, s)          # inline: only `changed` files extract
                else:
                    warnings.append(
                        f"!!! STALE SOURCE '{s.name}' !!! {changed} new/changed files exceed the "
                        f"auto-reindex budget ({budget}) — searching previously indexed content "
                        f"only. Recover: run reindex('{s.name}').")
            else:  # git_repo — always auto-reindex, budget doesn't apply
                result = reindex_source(conn, s)
                if isinstance(result, dict) and result.get("error"):
                    warnings.append(
                        f"!!! AUTO-REINDEX FAILED '{s.name}' !!! {result['error']} — "
                        f"searching previously indexed content only. "
                        f"Recover: run reindex('{s.name}').")
        except Exception as e:
            warnings.append(
                f"!!! AUTO-REINDEX FAILED '{s.name}' !!! {type(e).__name__}: {e} — searching "
                f"previously indexed content only. Recover: run reindex('{s.name}').")
    try:
        hits = search_index(conn, query, source=source, limit=max_files,
                            max_locations=max_locations)
    except sqlite3.OperationalError as e:                # corrupt/missing/locked index
        hits = []
        warnings.append(
            f"!!! INDEX ERROR !!! {e} — the search index looks corrupt or busy. "
            f"Recover: run reindex() to rebuild it.")
    resp: dict = {"results": hits, "sources_searched": [s.name for s in sources]}
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
    resp: dict = {"reindexed": [reindex_source(conn, s) for s in targets]}
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
    """List configured sources with indexed-file and unique-content counts (query-only).

    git_repo sources also report their url, the ref being tracked (declared ref or the
    remote's default branch), and a git_status in git's own terms: "up to date" (indexed
    commit == origin/ref after fetch), "behind" (origin/ref moved on), "dirty" (uncommitted
    changes in the working tree), "detached" (tracking a ref that doesn't resolve after
    fetch), "never indexed", or "unknown" (fetch/ref-resolution trouble). One bad source
    never breaks the rest: git failures degrade to a status string, never an exception.
    """
    sources, warnings = load_manifest()
    conn = get_index_db()
    out = []
    for s in sources:
        item: dict = {"name": s.name, "type": s.type,
                      "path": str(s.path) if s.path else str(_managed_repo_path(s.name)),
                      "mutable": s.mutable,
                      "indexed_files": conn.execute(
                          "SELECT COUNT(*) FROM locations WHERE source=?",
                          (s.name,)).fetchone()[0],
                      "unique_contents": conn.execute(
                          "SELECT COUNT(DISTINCT sha256) FROM locations WHERE source=?",
                          (s.name,)).fetchone()[0]}
        if s.type == "git_repo":
            item["url"] = s.url
            repo_path = s.path if s.path else _managed_repo_path(s.name)
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
                    clean, _ = _git_is_clean(repo_path)
                    if not clean:
                        item["git_status"] = "dirty"
                    else:
                        ref = s.ref or _default_branch(repo_path)
                        try:
                            _git_fetch(repo_path)
                            current = _git_resolve_ref(repo_path, ref)
                        except RuntimeError:
                            current = None

                        if current is None:
                            item["git_status"] = "detached" if s.ref else "unknown"
                        elif meta[0] == current:
                            item["git_status"] = "up to date"
                        else:
                            item["git_status"] = "behind"
                except Exception as e:
                    item["git_status"] = f"error: {e}"
        out.append(item)
    resp = {"sources": out}
    if warnings:
        resp["WARNING"] = warnings
    return resp


@mcp.tool()
def health_check() -> dict:
    """Check server health: corpus home, schema version, index DB, PDF extractors, sources."""
    status: dict = {"server": "rtfm", "ok": True, "issues": []}
    status["corpus_home"] = str(corpus_home())
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True, timeout=5)
        status["pdftotext"] = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        status["pdftotext"] = False
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
        if warnings:
            status["issues"].extend(warnings)
            status["ok"] = False
    except Exception as e:
        status["ok"] = False
        status["issues"].append(f"index/manifest error: {e}")
    return status


if __name__ == "__main__":
    mcp.run()
