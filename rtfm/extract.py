"""Text extraction: one routine per file family produces rows and a doc-level signal.

Text-extraction only — no OCR, no JS (ADR 0005). A failure yields an error
string, never an exception into the indexing pipeline.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from .config import CHUNK_LINES, MARKUP_EXTS

_log = logging.getLogger("rtfm")


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


def rows_for_file(path: Path) -> list[tuple[str, str, str]]:
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


class Routine(NamedTuple):
    """One handling routine: how a family of files becomes rows and a doc-level signal.

    Body and signal live in the same record so the two dispatches cannot disagree about
    which routine a file belongs to. Adding a format is adding a routine."""
    name: str
    exts: frozenset[str]
    rows: Callable[[Path], list[tuple[str, str, str]]]
    signal: Callable[[Path], tuple[str, str]]


ROUTINES: tuple[Routine, ...] = (
    Routine("pdf", frozenset({".pdf"}), _pdf_rows, _pdf_doc_signal),
    Routine("markup", MARKUP_EXTS, _text_rows, _text_doc_signal),
)

# The fallback sits outside ROUTINES. Inside, it would match unconditionally, so placing
# it anywhere but last would silently shadow every routine after it — .pdf quietly getting
# plain-text rows and no signal, with nothing raised. Out here there is no ordering to get
# wrong.
DEFAULT_ROUTINE = Routine("text", frozenset(), _text_rows, _no_doc_signal)


def _routine_for(ext: str) -> Routine:
    """The routine handling `ext`, else the plain-text fallback."""
    return next((r for r in ROUTINES if ext in r.exts), DEFAULT_ROUTINE)


def doc_signal_for_file(path: Path) -> tuple[str, str]:
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


def extract_rows(path_str: str) -> tuple[list[tuple[str, str, str]], str, str, str | None]:
    """Extraction worker, run in a thread by extract_many: extract (rows, title, headings) for
    one file. Returns (rows, title, headings, error); a failed body extraction yields
    ([], "", "", message) instead of raising."""
    try:
        path = Path(path_str)
        return rows_for_file(path), *doc_signal_for_file(path), None
    except Exception as e:
        return [], "", "", f"{type(e).__name__}: {e}"


class Extracted(NamedTuple):
    """One content's extraction result. Named so the two adjacent strings (title, headings) can't
    be swapped by positional unpacking downstream."""
    sha: str
    rows: list
    title: str
    headings: str
    error: str | None


def extract_many(jobs: list[tuple[str, str]]) -> list[Extracted]:
    """jobs: [(sha, path)] -> [Extracted(sha, rows, title, headings, error)]. Parallel over unique
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
    if len(jobs) == 1 or workers() == 1:
        return [Extracted(sha, *extract_rows(path)) for (sha, path) in jobs]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: list[Extracted] = []
    with ThreadPoolExecutor(max_workers=workers()) as ex:
        futs = {ex.submit(extract_rows, path): sha for (sha, path) in jobs}
        for fut in as_completed(futs):
            out.append(Extracted(futs[fut], *fut.result()))
    return out


def workers() -> int:
    """Extraction worker count: RTFM_WORKERS, else min(cpus, 8)."""
    env = os.environ.get("RTFM_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return min(os.cpu_count() or 4, 8)


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
