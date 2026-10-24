"""The staged indexing pipeline: access -> select -> read -> handle (ADR 0015).

Pure pipeline stages over a Source. This module owns the step result types and
their reporting; the index module owns the content-addressed store that persists
what these stages produce.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat as stat_mod
from pathlib import Path
from typing import NamedTuple

from . import extract
from .config import DEFAULT_EXT_BLOCKLIST, manifest_path
from .manifest import Source


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


def is_clean(r: StepResult) -> bool:
    return bool(r.kept) and not r.problems


def is_partial(r: StepResult) -> bool:
    return bool(r.kept) and bool(r.problems)


def is_empty(r: StepResult) -> bool:
    return not r.kept


class Reached(NamedTuple):
    """Step 1's product. The mtime is step 1's own stat — the freshness fact, recorded where
    it is already paid for rather than re-fetched by a second walk (ADR 0015)."""
    path: Path
    mtime: float


class Hashed(NamedTuple):
    """Step 3A's product: a position that gave up its bytes, and their hash."""
    path: Path
    sha: str
    mtime: float


class Scan(NamedTuple):
    """Steps 1 and 2 together. One value because its two consumers — the freshness handler
    that decides and the ingest that acts — must see the same walk (ADR 0015)."""
    reachable: StepResult
    wanted: list[Reached]


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


def select(positions: list[Reached], base: Path, src: Source) -> list[Reached]:
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
        if any(under(rel, p) for p in src.exclude_paths):
            return False
        return not src.paths or any(under(rel, p) for p in src.paths)

    return [r for r in positions
            if wanted_ext(r.path.suffix.lower()) and wanted_path(rel(r.path, base))]


def under(rel: str, prefix: str) -> bool:
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
    `Reached`, so the freshness handler decides from this walk (ADR 0015)."""
    base = root if root is not None else src.path
    if base is None or not base.exists():
        return StepResult([], [])
    kept: list[Reached] = []
    problems: list[Problem] = []

    def _denied(err: OSError) -> None:
        if err.filename:
            problems.append(Problem(
                rel(Path(err.filename), base), err.strerror or type(err).__name__))

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
                kept.append(Reached(f, st.st_mtime))
            else:
                problems.append(Problem(rel(f, base), "Permission denied"))
    return StepResult(sorted(kept), problems)


def scan(src: Source, root: Path) -> Scan:
    """Steps 1 and 2 over one source, and the only place `access` is called on the indexing
    path — so a query walks once, however many consumers the result has."""
    reachable = access(src, root)
    return Scan(reachable, select(reachable.kept, root, src))


def rel(p: Path, base: Path) -> str:
    """A position: the path as the user wrote it, relative to the source root."""
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return str(p)


def still_present(indexed: dict[str, float], scan: Scan, root: Path) -> dict[str, float]:
    """Positions this source still has, and their mtimes: what step 2 wants, plus indexed
    rows sitting behind something step 1 could not enter.

    Unreadable is not absent. A position denied by step 1 never reaches step 2, so asking
    step 2 alone would call it vanished, delete its row and GC its content because someone
    ran chmod. Freshness and the ingest both need this exact set — freshness to stop going
    stale forever, the ingest to stop purging — and one function is how they agree."""
    present = {rel(r.path, root): r.mtime for r in scan.wanted}
    unreachable = {p.position for p in scan.reachable.problems}
    for position, mtime in indexed.items():
        if position in present or not shadowed_by(position, unreachable):
            continue
        try:
            present[position] = (root / position).stat().st_mtime
        except OSError:
            present[position] = mtime      # behind an unreachable ancestor: can't re-read
    return present


def shadowed_by(rel: str, unreachable: set[str]) -> bool:
    """Does a position step 1 could not reach sit on this relpath's way down?

    `rel` renders the source root as ".", which is the one position no relpath equals or
    is prefixed by, so a denied root has to be matched on its own terms. Miss it and every
    indexed row of that source reads as vanished while every file is still on disk.

    Both reconcilers ask this, and they have to agree: index_source deletes rows on a False
    and _stale_delta stops converging on one.
    """
    return any(u == "." or rel == u or rel.startswith(u + "/") for u in unreachable)


def read_bytes_for(positions: list[Reached], base: Path,
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
    kept: list[Hashed] = []
    problems: list[Problem] = []
    for r in positions:
        position = rel(r.path, base)
        try:
            r.path.stat()                       # liveness: gone since the scan is step 3A's
            prev = existing.get(position)
            sha = prev[0] if prev and prev[1] == r.mtime else \
                hashlib.sha256(r.path.read_bytes()).hexdigest()
        except OSError as e:
            problems.append(Problem(position, e.strerror or type(e).__name__))
            continue
        kept.append(Hashed(r.path, sha, r.mtime))
    return StepResult(kept, problems)


def handle(read_kept: list[Hashed], need: set[str], base: Path) -> StepResult:
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
        by_sha.setdefault(h.sha, []).append(rel(h.path, base))
        if h.sha in need and h.sha not in jobs:
            jobs[h.sha] = str(h.path)

    kept: list[extract.Extracted] = []
    problems: list[Problem] = []
    for ex in extract.extract_many(list(jobs.items())):
        if ex.error:
            problems.extend(Problem(pos, ex.error) for pos in sorted(by_sha.get(ex.sha, [])))
        kept.append(ex)
    return StepResult(kept, sorted(problems))


