"""Query-side tools: search, read, reindex, duplicates, sources, and health.

Plain functions over the store; `rtfm.tools` registers them with the MCP app.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess

from . import extract, git_ops, index, staging
from .config import MAX_LOCATIONS, corpus_home, managed_repo_path, manifest_path
from .manifest import Source, load_manifest

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
        return extract.extract_pdf_text(path, start=start, end=end)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as dec_err:
        return (f"!!! CANNOT READ '{relpath}' !!! not valid UTF-8 ({dec_err.reason} at byte "
                f"{dec_err.start}). rtfm indexes UTF-8 text. Recover: convert the file, or "
                f"exclude its type in {manifest_path()}.")
    e = end if end is not None else len(lines)
    return "\n".join(lines[max(0, start - 1):e])


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
    conn = index.get_index_db()
    budget = index.auto_reindex_max()
    warned_repo_paths: set[str] = set()      # one warning per clone, not per source
    for s in sources:
        if source is not None and s.name != source:
            continue
        if s.type not in ("dir", "git_repo"):
            continue
        try:                                         # one source's refresh never fails the query
            verdict = index.freshness(conn, s)
            if s.type == "dir":
                result = None
                if verdict.stale and verdict.changed <= budget:
                    # Re-uses the freshness handler's walk: one traversal per query.
                    result = index.ingest(conn, s, s.path, verdict.scan)
                elif verdict.stale:
                    warnings.append(
                        f"!!! STALE SOURCE '{s.name}' !!! {verdict.changed} new/changed files "
                        f"exceed the auto-reindex budget ({budget}) — searching previously "
                        f"indexed content only. Recover: run reindex('{s.name}').")
                if result is None and verdict.scan is not None:
                    # Fresh, or too big to index inline: steps 1 and 2 still have something to
                    # say and the scan is in hand. An empty 3A/3B tail keeps `report_all` the
                    # one reporting path (ADR 0015).
                    result = index.EMPTY_INDEXED._replace(
                        reachable=verdict.scan.reachable, wanted=verdict.scan.wanted)
                if result is not None:
                    warnings.extend(staging.report_all(conn, s, result))
                continue
            # Not reindexing this query: no scan to report from, but the store answers free.
            if not verdict.stale:
                warnings.extend(staging.report_unsearchable(conn, s.name))
                continue
            if verdict.cached:
                # The verdict came from the memo (<= STALENESS_TTL old): the reindex
                # attempt and its failure warning run once per window, not on every
                # query — a persistently broken or dirty source must not block or
                # spam every search (ADR 0013).
                warnings.extend(staging.report_unsearchable(conn, s.name))
                continue
            # git_repo — always auto-reindex, budget doesn't apply
            summary = index.reindex_source(conn, s)
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


def reindex(source: str | None = None) -> dict:
    """Build/refresh the index. The ONLY tool that extracts. Pass a source name to rebuild
    just that source, or omit to rebuild all dir and git_repo sources. Returns a per-source
    summary."""
    sources, warnings = load_manifest()
    conn = index.get_index_db()
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
            for key in [k for k in index.freshness_cache if k[0] == name]:
                index.freshness_cache.pop(key, None)
        if dropped:
            conn.commit()
    resp: dict = {"reindexed": [index.reindex_source(conn, s) for s in targets]}
    for summary in resp["reindexed"]:
        warnings.extend(summary.pop("warnings", ()))
    if dropped:
        resp["purged_sources"] = dropped
    if warnings:
        resp["WARNING"] = warnings
    return resp


def find_duplicates(source: str | None = None, min_locations: int = 2) -> dict:
    """List contents stored at multiple paths (byte-identical files under different paths or
    versions). With `source`, reports contents duplicated WITHIN that source — the count and
    listed paths are scoped to it. Without `source`, reports duplicates across the whole
    corpus. Groups are sorted by location count, descending."""
    conn = index.get_index_db()
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
    conn = index.get_index_db()
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
                item["ref"] = s.ref or index.default_branch(repo_path)
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
                        item["git_status"] = index.linked_git_status(repo_path, s, meta[0])
                    else:
                        item["git_status"] = index.managed_git_status(repo_path, s, meta[0])
                except Exception as e:
                    item["git_status"] = f"error: {e}"
        out.append(item)
    resp = {"sources": out}
    if warnings:
        resp["WARNING"] = warnings
    return resp


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
        conn = index.get_index_db()
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
            reachable = staging.access(src, root)
            issues = staging.report(src.name, reachable, "access")
            if issues:
                status["ok"] = False
                status["issues"].extend(issues)
        # Asking per manifest source also skips rows left behind by a source since deleted
        # from the manifest, with no name filtering of its own.
        for src in sources:
            issues = staging.report_unsearchable(conn, src.name)
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


