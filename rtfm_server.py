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
import os
import re
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# --- config -----------------------------------------------------------------
TEXT_EXTS = {".txt", ".md", ".rst", ".rest"}   # plain text → line locators (.html later)
CHUNK_LINES = 50
SCHEMA_VERSION = 2                   # index DB is a cache; mismatch ⇒ drop & rebuild
MAX_LOCATIONS = 5                    # default cap on locations listed per search hit


def corpus_home() -> Path:
    return Path(os.environ.get("RTFM_HOME", Path.home() / ".rtfm")).expanduser()


def manifest_path() -> Path:
    return corpus_home() / "manifest.toml"


def default_source_dir() -> Path:
    return corpus_home() / "default"


def index_db_path() -> Path:
    return corpus_home() / "cache" / "index.db"

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


def _extract_rows(path_str: str) -> tuple[list[tuple[str, str, str]], str | None]:
    """Extraction worker, run in a thread by _extract_many: extract rows for one file.
    Returns (rows, error); a failed extraction yields ([], message) instead of raising."""
    try:
        return _rows_for_file(Path(path_str)), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _extract_many(jobs: list[tuple[str, str]]) -> list[tuple[str, list, str | None]]:
    """jobs: [(sha, representative_path)] -> [(sha, rows, error)]. Parallel across unique
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
        return [(sha, *_extract_rows(path)) for (sha, path) in jobs]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: list[tuple[str, list, str | None]] = []
    with ThreadPoolExecutor(max_workers=_workers()) as ex:
        futs = {ex.submit(_extract_rows, path): sha for (sha, path) in jobs}
        for fut in as_completed(futs):
            rows, error = fut.result()
            out.append((futs[fut], rows, error))
    return out


def reindex_source(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild one dir source: dedup by content hash, extract each unique content once, map
    every path to its content, purge vanished files, GC orphaned contents. Returns a summary.

    extraction_skips = files_seen - unique contents that required fresh extraction this run.
    Covers two cases: byte-identical duplicates within the same run (same sha, only one job
    submitted) and contents already extracted in a prior run (cache hits in `contents`).
    Value: files_seen - len(need), where need = unique shas not yet in contents.
    """
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

    for sha, rows, error in _extract_many(list(jobs.items())):
        kind = rows[0][0] if rows else "line"
        conn.execute("DELETE FROM content_fts WHERE sha256=?", (sha,))
        conn.executemany(
            "INSERT INTO content_fts(sha256, locator_kind, locator_value, text) VALUES(?,?,?,?)",
            [(sha, k, v, t) for (k, v, t) in rows])
        conn.execute(
            "INSERT INTO contents(sha256, locator_kind, n_chunks, extracted_ok, error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET "
            "locator_kind=excluded.locator_kind, n_chunks=excluded.n_chunks, "
            "extracted_ok=excluded.extracted_ok, error=excluded.error",
            (sha, kind, len(rows), 0 if error else 1, error))
        summary["errors" if error else "newly_extracted"] += 1
    conn.commit()

    conn.execute(
        "DELETE FROM content_fts WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM locations)")
    conn.execute(
        "DELETE FROM contents WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM locations)")
    conn.commit()
    return summary


def _stale_delta(conn: sqlite3.Connection, src: Source) -> tuple[int, bool]:
    """Cheap staleness check for any dir source — stat + (relpath, mtime) compare, no hashing
    or extraction. Returns (changed, stale):

      changed  count of new or mtime-changed files — the ones that would need fresh extraction.
               Bounds the cost of an inline auto-reindex (see _auto_reindex_max).
      stale    whether the index differs from disk at all, including files that vanished on
               disk (which a reindex purges essentially for free, so they don't inflate `changed`).
    """
    if src.path is None or not src.path.exists():
        return 0, False
    indexed = {r[0]: r[1] for r in conn.execute(
        "SELECT relpath, mtime FROM locations WHERE source=?", (src.name,))}
    on_disk = {str(f.relative_to(src.path)): f.stat().st_mtime for f in iter_source_files(src)}
    changed = sum(1 for rel, mtime in on_disk.items() if indexed.get(rel) != mtime)
    stale = changed > 0 or set(indexed) != set(on_disk)   # latter catches vanished files
    return changed, stale


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
    type: str                       # "dir" (a "repo" type is added later)
    path: Path | None = None
    url: str | None = None
    mutable: bool = False
    refresh: bool = True            # repo-only; ignored for dir


_BOOTSTRAP_MANIFEST = '''\
# rtfm source manifest. See manifest.example.toml in the repo for all options.
# Each [[source]] is one place rtfm indexes, in place.

[[source]]
name    = "default"   # the zero-config drop-dir; the only mutable source by default
type    = "dir"
path    = "{default}"
mutable = true
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
        mutable=bool(t.get("mutable", False)),
        refresh=bool(t.get("refresh", True)),
    )


def _validate_source(s: Source) -> str | None:
    """Loud warning if a source is misconfigured, else None. A dir source needs an existing,
    readable directory; a `path`-less dir source is unusable (the caller drops it). The point is
    that one bad entry never silently disappears and never breaks the others."""
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


def search_index(conn: sqlite3.Connection, query: str, source: str | None = None,
                 limit: int = 20, max_locations: int = MAX_LOCATIONS) -> list[dict]:
    """Dedup'd hits keyed by content sha. AND-first, then OR/BM25 fallback. Each hit lists up
    to `max_locations` of the paths its content lives at, plus total_locations."""
    q = query.strip()
    if not q:
        return []
    sanitized = _sanitize_fts(q)
    base = "SELECT sha256, locator_kind, locator_value, text FROM content_fts WHERE text MATCH ?"
    params: list = [sanitized]
    if source is not None:
        base += " AND sha256 IN (SELECT sha256 FROM locations WHERE source=?)"
        params.append(source)
    try:
        rows = conn.execute(base + " LIMIT ?", (*params, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    terms = [t for t in sanitized.split() if len(t) >= 3] or sanitized.split()
    if not rows and len(terms) > 1:
        params[0] = " OR ".join(terms)
        try:
            rows = conn.execute(base + " ORDER BY bm25(content_fts) LIMIT ?",
                                (*params, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    qterms = [t.lower() for t in sanitized.split() if len(t) > 1]
    hits = []
    for sha, kind, val, text in rows:
        snippet = next((ln.strip() for ln in text.split("\n")
                        if any(t in ln.lower() for t in qterms)),
                       text.strip().split("\n")[0])
        locs = conn.execute(
            "SELECT source, relpath FROM locations WHERE sha256=? ORDER BY source, relpath LIMIT ?",
            (sha, max_locations)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM locations WHERE sha256=?", (sha,)).fetchone()[0]
        hits.append({"sha256": sha, "locator_kind": kind, "locator_value": val,
                     "snippet": snippet[:_SNIPPET_CAP],
                     "locations": [{"source": s, "relpath": r} for (s, r) in locs],
                     "total_locations": total})
    return hits


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
    reindex() and reported in WARNING rather than blocking the query on extraction. This
    refreshes the search cache only — it never mutates the source files.

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
        if s.type != "dir" or (source is not None and s.name != source):
            continue
        try:                                         # one source's refresh never fails the query
            changed, stale = _stale_delta(conn, s)
            if not stale:
                continue
            if changed <= budget:
                reindex_source(conn, s)              # inline: only `changed` files extract
            else:
                warnings.append(
                    f"!!! STALE SOURCE '{s.name}' !!! {changed} new/changed files exceed the "
                    f"auto-reindex budget ({budget}) — searching previously indexed content "
                    f"only. Recover: run reindex('{s.name}').")
        except Exception as e:
            warnings.append(
                f"!!! AUTO-REINDEX FAILED '{s.name}' !!! {type(e).__name__}: {e} — searching "
                f"previously indexed content only. Recover: run reindex('{s.name}').")
    hits = search_index(conn, query, source=source, limit=max_files, max_locations=max_locations)
    resp: dict = {"results": hits, "sources_searched": [s.name for s in sources]}
    if warnings:
        resp["WARNING"] = warnings
    if not query.strip():
        resp["error"] = "Query must be non-empty."
    return resp


@mcp.tool()
def reindex(source: str | None = None) -> dict:
    """Build/refresh the index. The ONLY tool that extracts. Pass a source name to rebuild
    just that source, or omit to rebuild all dir sources. Returns a per-source summary."""
    sources, warnings = load_manifest()
    conn = get_index_db()
    targets = [s for s in sources if s.type == "dir" and (source is None or s.name == source)]
    if source is not None and not targets:
        return {"error": f"source '{source}' not found or not a dir source. Call list_sources()."}
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
    """List configured sources with indexed-file and unique-content counts (query-only)."""
    sources, warnings = load_manifest()
    conn = get_index_db()
    out = []
    for s in sources:
        files = conn.execute("SELECT COUNT(*) FROM locations WHERE source=?",
                             (s.name,)).fetchone()[0]
        uniq = conn.execute("SELECT COUNT(DISTINCT sha256) FROM locations WHERE source=?",
                            (s.name,)).fetchone()[0]
        out.append({"name": s.name, "type": s.type,
                    "path": str(s.path) if s.path else s.url,
                    "mutable": s.mutable, "indexed_files": files, "unique_contents": uniq})
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
        status["sources"] = [{"name": s.name, "type": s.type} for s in sources]
        if warnings:
            status["issues"].extend(warnings)
            status["ok"] = False
    except Exception as e:
        status["ok"] = False
        status["issues"].append(f"index/manifest error: {e}")
    return status


if __name__ == "__main__":
    mcp.run()
