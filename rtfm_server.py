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
TEXT_EXTS = {".txt", ".md"}          # more text/markup formats (e.g. .html) added later
CHUNK_LINES = 50


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
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS doc_meta (
            source  TEXT NOT NULL,
            relpath TEXT NOT NULL,
            mtime   REAL NOT NULL,
            sha256  TEXT,
            PRIMARY KEY (source, relpath)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
            source UNINDEXED, relpath UNINDEXED,
            locator_kind UNINDEXED, locator_value UNINDEXED,
            text
        );
        """
    )
    conn.commit()
    return conn


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


def index_file(
    conn: sqlite3.Connection, source: str, path: Path, root: Path
) -> tuple[bool, str | None]:
    ext = path.suffix.lower()
    if ext != ".pdf" and ext not in TEXT_EXTS:
        return False, f"unsupported extension {ext}"
    rel = str(path.relative_to(root))
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    mtime = path.stat().st_mtime
    rows = _rows_for_file(path)
    conn.execute("DELETE FROM doc_fts WHERE source=? AND relpath=?", (source, rel))
    conn.execute("DELETE FROM doc_meta WHERE source=? AND relpath=?", (source, rel))
    conn.executemany(
        "INSERT INTO doc_fts(source, relpath, locator_kind, locator_value, text) "
        "VALUES (?,?,?,?,?)",
        [(source, rel, k, v, t) for (k, v, t) in rows],
    )
    conn.execute(
        "INSERT INTO doc_meta(source, relpath, mtime, sha256) VALUES (?,?,?,?)",
        (source, rel, mtime, sha),
    )
    conn.commit()
    return True, None


def ensure_indexed(conn: sqlite3.Connection, source: str, path: Path, root: Path) -> str:
    """Index if new/changed (by mtime). Returns 'indexed' or 'skipped'."""
    rel = str(path.relative_to(root))
    row = conn.execute(
        "SELECT mtime FROM doc_meta WHERE source=? AND relpath=?", (source, rel)
    ).fetchone()
    if row is None or row[0] != path.stat().st_mtime:
        index_file(conn, source, path, root)
        return "indexed"
    return "skipped"


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


def index_source(conn: sqlite3.Connection, src: Source) -> int:
    """Index/refresh all supported files in a source, purge deleted ones. Returns file count."""
    if src.path is None:
        return 0
    present = {str(f.relative_to(src.path)) for f in iter_source_files(src)}
    indexed = {r[0] for r in conn.execute(
        "SELECT relpath FROM doc_meta WHERE source=?", (src.name,)).fetchall()}
    for rel in indexed - present:
        conn.execute("DELETE FROM doc_fts WHERE source=? AND relpath=?", (src.name, rel))
        conn.execute("DELETE FROM doc_meta WHERE source=? AND relpath=?", (src.name, rel))
    conn.commit()
    for f in iter_source_files(src):
        ensure_indexed(conn, src.name, f, src.path)
    return len(present)

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


def load_manifest() -> tuple[list[Source], list[str]]:
    """Return (sources, warnings). Bootstraps a default manifest if none exists.

    Duplicate names are resolved first-wins; each refused duplicate yields a loud,
    actionable warning string (ADR 0006). Never raises on duplicates.
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
                 limit: int = 50) -> list[dict]:
    """Return hits as {source, relpath, locator_kind, locator_value, snippet}.

    AND first (all terms in one chunk), OR/BM25 fallback for multi-term queries.
    """
    q = query.strip()
    if not q:
        return []
    sanitized = _sanitize_fts(q)
    where = "text MATCH ?"
    params: list = [sanitized]
    if source:
        where = "source = ? AND " + where
        params = [source, sanitized]
    try:
        rows = conn.execute(
            f"SELECT source, relpath, locator_kind, locator_value, text FROM doc_fts "
            f"WHERE {where} LIMIT ?", (*params, limit)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    terms = [t for t in sanitized.split() if len(t) >= 3] or sanitized.split()
    if not rows and len(terms) > 1:
        or_q = " OR ".join(terms)
        params[-1] = or_q
        try:
            rows = conn.execute(
                f"SELECT source, relpath, locator_kind, locator_value, text FROM doc_fts "
                f"WHERE {where} ORDER BY bm25(doc_fts) LIMIT ?", (*params, limit)
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    hits = []
    qterms = [t.lower() for t in sanitized.split() if len(t) > 1]
    for s, rel, kind, val, text in rows:
        snippet = next(
            (ln.strip() for ln in text.split("\n")
             if any(t in ln.lower() for t in qterms)), text.strip().split("\n")[0]
        )
        hits.append({
            "source": s, "relpath": rel, "locator_kind": kind,
            "locator_value": val, "snippet": snippet[:_SNIPPET_CAP],
        })
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


def _load_and_index() -> tuple[list[Source], list[str], sqlite3.Connection]:
    sources, warnings = load_manifest()
    conn = get_index_db()
    for s in sources:
        if s.type == "dir":
            index_source(conn, s)
    return sources, warnings, conn


@mcp.tool()
def search(query: str, source: str | None = None, max_files: int = 20) -> dict:
    """Search the corpus. Returns hits with format-native locators (page/line).

    Args:
        query: text to search for.
        source: restrict to one source name (None = all).
        max_files: cap on returned hits.
    """
    sources, warnings, conn = _load_and_index()
    hits = search_index(conn, query, source=source, limit=max_files)
    resp: dict = {"results": hits, "sources_searched": [s.name for s in sources]}
    if warnings:
        resp["WARNING"] = warnings
    if not query.strip():
        resp["error"] = "Query must be non-empty."
    return resp


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
    """List configured sources and their indexed file counts."""
    sources, warnings = load_manifest()
    conn = get_index_db()
    out = []
    for s in sources:
        n = conn.execute(
            "SELECT COUNT(DISTINCT relpath) FROM doc_meta WHERE source=?", (s.name,)
        ).fetchone()[0]
        out.append({"name": s.name, "type": s.type,
                    "path": str(s.path) if s.path else s.url,
                    "mutable": s.mutable, "indexed_files": n})
    resp = {"sources": out}
    if warnings:
        resp["WARNING"] = warnings
    return resp


@mcp.tool()
def health_check() -> dict:
    """Check server health: corpus home, index DB, PDF extractors, sources."""
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
        conn.execute("SELECT 1")
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
