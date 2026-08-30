#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.28.1,<2",
#   "pymupdf",
#   "pypdf",
# ]
# ///
"""rtfm — Read The Full Manual. Local document-corpus search MCP server."""
from __future__ import annotations

import hashlib
import html.parser
import logging
import os
import re
import sqlite3
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP

# --- config -----------------------------------------------------------------
_log = logging.getLogger("rtfm")

TEXT_EXTS = {".txt", ".md", ".rst", ".rest", ".html"}   # text → line locators; html → main-region text
CHUNK_LINES = 50
SCHEMA_VERSION = 5                   # index DB is a cache; mismatch ⇒ drop & rebuild
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

# --- web ---------------------------------------------------------------------

_WEB_FETCH_DELAY = 1.0        # politeness gap between page fetches (tests set 0)
_WEB_TIMEOUT = 20             # socket timeout per request, seconds
_WEB_MAX_PAGES_DEFAULT = 2000


def _web_cache_path(name: str) -> Path:
    """Cache dir for a web source: raw HTML mirroring the URL tree under the version root."""
    return corpus_home() / "web" / name


def _web_max_pages() -> int:
    """Hard cap on pages fetched per web reindex. RTFM_WEB_MAX_PAGES overrides."""
    env = os.environ.get("RTFM_WEB_MAX_PAGES")
    if env and env.isdigit():
        return int(env)
    return _WEB_MAX_PAGES_DEFAULT


def _web_url_parts(url: str) -> tuple[str, str, str]:
    """(scheme, netloc, version_root_path) for a web source's index URL.

    The version root is the index URL's directory, normalized to end with '/':
    'https://docs.ansible.com/projects/ansible/latest/index.html' →
    ('https', 'docs.ansible.com', '/projects/ansible/latest/'). The crawl scope
    is everything under it. Raises ValueError for a non-http(s) or malformed URL
    (reported as FETCH_FAILED at reindex time, never a load-time gate)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    if "/" not in u.path:
        raise ValueError(f"URL has no path: {url!r}")
    root = u.path.rsplit("/", 1)[0].rstrip("/") + "/"
    return u.scheme, u.netloc, root


_last_fetch = 0.0


def _throttle() -> None:
    """Enforce at least _WEB_FETCH_DELAY seconds between fetches (politeness: RTD
    custom domains rate-limit aggressively — docs.ansible.com 429s under light load)."""
    global _last_fetch
    now = time.monotonic()
    gap = _WEB_FETCH_DELAY - (now - _last_fetch)
    if gap > 0:
        time.sleep(gap)
    _last_fetch = time.monotonic()


_urlopen = urllib.request.urlopen          # seam for tests


def _http_get(url: str) -> tuple[bytes | None, str | None]:
    """Fetch one URL. (body, None) on success; (None, classified error) on failure.
    Classification order matters: a Cloudflare challenge body ('Just a moment...',
    'cf_chl', 'challenge-platform') marks BLOCKED regardless of status code — the
    real wall returns both 403 and 429; RATE_LIMITED is the bare-429 case; anything
    else 4xx/5xx and transport failures are FETCH_FAILED."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "rtfm (doc corpus fetcher; cooperative-only)"})
    try:
        with _urlopen(req, timeout=_WEB_TIMEOUT) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            pass
        low = body.lower()
        if "just a moment" in low or "cf_chl" in low or "challenge-platform" in low:
            return None, (
                "ERROR:BLOCKED: the host serves a bot-protection challenge "
                "(Cloudflare-style) instead of content. Recover: this host walls off "
                "scripted fetching — try a git_repo source for the project's docs "
                "repo, or remove the source.")
        if e.code == 429:
            return None, (
                f"ERROR:RATE_LIMITED: HTTP 429 from {url}. Recover: wait a while, "
                f"then reindex this source again.")
        return None, (
            f"ERROR:FETCH_FAILED: HTTP {e.code} from {url}. Recover: check the URL "
            f"and network, then reindex.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, (
            f"ERROR:FETCH_FAILED: {e} fetching {url}. Recover: check the URL and "
            f"network, then reindex.")


def _fetch_page(url: str) -> tuple[bytes | None, str | None]:
    """Throttled page fetch — the one seam discovery/reindex use."""
    _throttle()
    return _http_get(url)


_WEB_EXCLUDED_NAMES = {"search.html", "genindex.html", "py-modindex.html", "404.html", "llms.txt"}


def _html_hrefs(html_text: str) -> list[str]:
    # Parameter deliberately NOT named `html` — it would shadow the html module
    # and break `html.parser.HTMLParser` inside the class body.
    class _Links(html.parser.HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hrefs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag == "a":
                for k, v in attrs:
                    if k == "href" and v:
                        self.hrefs.append(v)

    p = _Links()
    p.feed(html_text)
    p.close()
    return p.hrefs


def _llms_links(text: str) -> list[str]:
    """Markdown link destinations from an llms.txt body."""
    return re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)


def _web_pages(index_url: str, hrefs: list[str]) -> list[str]:
    """Same-version page paths (relative to the version root) from hrefs.

    A link is a page iff it resolves (urljoin against the index URL) to the same
    netloc, its path starts with the version root, and its basename is not an
    excluded artifact (search/genindex/etc.). Paths normalize to cache-relative
    form: '/projects/widget/latest/tutorial/' → 'tutorial/index.html',
    '/projects/widget/latest/' → 'index.html', 'guide.html' stays. Deduped,
    sorted."""
    scheme, netloc, root = _web_url_parts(index_url)
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        u = urllib.parse.urljoin(index_url, href.strip())
        p = urllib.parse.urlsplit(u)
        if p.scheme not in ("http", "https") or p.netloc != netloc:
            continue
        path = p.path
        if not path.startswith(root):
            continue
        if "/_static/" in path or "/_sources/" in path:
            continue
        if path.rsplit("/", 1)[-1] in _WEB_EXCLUDED_NAMES:
            continue
        canon = path[len(root):].rstrip("/")
        if canon.endswith(".html"):
            pass
        elif canon == "":
            canon = "index.html"
        else:
            canon = canon + "/index.html"
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return sorted(out)


def _web_discover(fetch, index_url: str) -> tuple[list[str], str | None]:
    """Page list for a readthedocs flavor source. llms.txt fast-path first: one
    fetch yields the complete page list when the project opts in (most don't —
    all probes 404'd, so the crawl is the common path). The crawl harvests the
    index page's links (the RTD sidebar carries the full toctree) and verifies
    the index page is actually RTD-shaped: a page with no div[role="main"] is
    the trust-the-URL failure signal (NOT_READTHEDOCS), and we refuse to crawl
    an arbitrary site."""
    llms_url = urllib.parse.urljoin(index_url, "llms.txt")
    data, err = fetch(llms_url)
    if err is None and data:
        pages = _web_pages(index_url, _llms_links(data.decode(errors="replace")))
        if pages:
            return pages, None
    data, err = fetch(index_url)
    if err is not None:
        return [], err
    html = data.decode(errors="replace")
    title, _, lines = _html_to_text(html)
    if not lines:
        return [], (
            "ERROR:NOT_READTHEDOCS: the page has no div[role=\"main\"] content — "
            "this doesn't look like a ReadTheDocs site. Recover: check the url in "
            "the manifest, or use a different source type.")
    pages = _web_pages(index_url, _html_hrefs(html))
    return [p for p in pages if p != "index.html"], None


def _sitemap_lastmod(data: bytes, version_root: str) -> str | None:
    """The <lastmod> of the version-index sitemap entry whose <loc> path matches
    version_root — a cheap 'upstream built at' timestamp. None when the sitemap
    doesn't parse or has no matching entry (custom sitemaps; the skip-optimization
    degrades to always-crawl)."""
    text = data.decode(errors="replace")
    for block in re.findall(r"<url>(.*?)</url>", text, re.S):
        loc = re.search(r"<loc>(.*?)</loc>", block, re.S)
        lm = re.search(r"<lastmod>(.*?)</lastmod>", block, re.S)
        if loc and lm:
            path = urllib.parse.urlsplit(loc.group(1).strip()).path.rstrip("/") + "/"
            if path == version_root:
                return lm.group(1).strip()
    return None


def _web_version(version_root: str) -> str:
    """Best-effort version name: the last path segment of the version root
    ('/projects/ansible/latest/' → 'latest'). The URL is the identity; this is
    display metadata only."""
    return version_root.rstrip("/").rsplit("/", 1)[-1]


def _web_meta(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT url, version, fetched_at, page_count, total_pages, lastmod, status, error "
        "FROM web_meta WHERE source=?", (name,)).fetchone()
    if row is None:
        return None
    return {"url": row[0], "version": row[1], "fetched_at": row[2], "page_count": row[3],
            "total_pages": row[4], "lastmod": row[5], "status": row[6], "error": row[7]}


def _web_meta_write(conn: sqlite3.Connection, src: Source, *, version: str,
                    fetched_at: float, page_count: int, total_pages: int,
                    lastmod: str | None, status: str, error: str | None) -> None:
    conn.execute(
        "INSERT INTO web_meta(source, url, version, fetched_at, page_count, total_pages, "
        "lastmod, status, error) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET url=excluded.url, version=excluded.version, "
        "fetched_at=excluded.fetched_at, page_count=excluded.page_count, "
        "total_pages=excluded.total_pages, lastmod=excluded.lastmod, "
        "status=excluded.status, error=excluded.error",
        (src.name, src.url or "", version, fetched_at, page_count, total_pages,
         lastmod, status, error))
    conn.commit()


def _web_fail(conn: sqlite3.Connection, src: Source, error: str) -> dict:
    """Record a failed reindex (status='error') and return the error summary. Prior
    cache and index rows are left untouched — prior content stays searchable and
    the failure is loud via web_status and search's sources_failed/warning."""
    try:
        _, _, root = _web_url_parts(src.url or "")
    except ValueError:
        root = "/"
    _web_meta_write(conn, src, version=_web_version(root), fetched_at=time.time(),
                    page_count=0, total_pages=0, lastmod=None, status="error", error=error)
    return {"source": src.name, "error": error}


def _reindex_web(conn: sqlite3.Connection, src: Source) -> dict:
    """Fetch + index a web source. Flow:
    1. Probe the domain-root sitemap's per-version lastmod. If the previous run
       completed (status ok/truncated) and lastmod is unchanged → "up to date",
       skip the crawl entirely. CDN-lagged or unparseable sitemaps degrade to crawl.
    2. Discover the page list (llms.txt fast-path, else the index-page nav crawl).
    3. Fetch each page (throttled, capped at _web_max_pages); a host-level failure
       (BLOCKED/RATE_LIMITED) aborts; an individual page 404/failure is skipped and
       counted. Write raw HTML into the cache mirroring the URL tree.
    4. Purge cache files no longer in the page set; hand the cache to the shared
       _index_files core (content-addressed → unchanged pages cost nothing).
    5. Record web_meta: version, page_count, total_pages, lastmod, status."""
    try:
        scheme, netloc, root = _web_url_parts(src.url or "")
    except ValueError as e:
        return _web_fail(conn, src, f"ERROR:FETCH_FAILED: {e}. Recover: fix 'url' "
                                      f"in the manifest.")
    cache = _web_cache_path(src.name)
    cache.mkdir(parents=True, exist_ok=True)
    meta = _web_meta(conn, src.name)

    data, err = _fetch_page(f"{scheme}://{netloc}/sitemap.xml")
    lastmod = _sitemap_lastmod(data, root) if err is None and data else None
    if (err is None and meta and meta["status"] in ("ok", "truncated")
            and lastmod is not None and lastmod == meta["lastmod"]):
        return {"source": src.name, "status": "up to date", "lastmod": lastmod}

    pages, err = _web_discover(_fetch_page, src.url or "")
    if err is not None:
        return _web_fail(conn, src, err)

    total = len(set(pages) | {"index.html"})   # the seed page is part of the site
    limit = _web_max_pages()
    truncated = total > limit
    targets = sorted(set(pages) | {"index.html"})[:limit]

    written: set[str] = set()
    failed_pages = 0
    for rel in targets:
        page_url = urllib.parse.urljoin(src.url or "", rel)
        data, ferr = _fetch_page(page_url)
        if ferr is not None:
            if ferr.startswith(("ERROR:BLOCKED:", "ERROR:RATE_LIMITED:")):
                return _web_fail(conn, src, ferr)   # host-level condition — abort
            failed_pages += 1                        # per-page flake — skip, count
            continue
        out_path = cache / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        written.add(rel)

    # Purge cache files from previous runs that are no longer in the page set.
    if written:
        for f in cache.rglob("*"):
            if f.is_file() and f.relative_to(cache).as_posix() not in written:
                f.unlink()

    summary: dict = {"source": src.name, "pages_fetched": len(written),
                     "pages_failed": failed_pages, "truncated": truncated,
                     "total_pages": total}
    if failed_pages:
        summary["warning"] = (f"{failed_pages} page(s) failed to fetch and were "
                              f"skipped — the index may be missing some pages.")
    summary.update(_index_files(conn, src.name, cache))
    _web_meta_write(conn, src, version=_web_version(root), fetched_at=time.time(),
                    page_count=len(written), total_pages=total, lastmod=lastmod,
                    status="truncated" if truncated else "ok", error=None)
    return summary

# --- git operations ----------------------------------------------------------

def _git(args: list[str], cwd: Path, timeout: int | None = None,
         allow_exit_codes: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """Run a git command. Raises RuntimeError on non-zero exit, timeout, or
    subprocess failure (missing/untraversable cwd, missing git binary, permissions).
    `allow_exit_codes` are non-zero exits that are legitimate answers, not failures
    (git merge-base --is-ancestor exits 1 for a genuine 'not an ancestor')."""
    t = timeout if timeout is not None else _git_timeout()
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


def _git_repo_root(path: Path) -> Path | None:
    """Return the git repo root, or None if `path` is not in a git repo.

    A missing git binary or any other git failure (corruption, permissions,
    ownership guard, timeout) raises RuntimeError — collapsing them to None would
    misreport a machine without git as NOT_GIT_REPO and a corrupt repo as "not a
    repo" with the wrong recovery advice. Only git's own "not a git repository"
    verdict returns None.
    """
    try:
        cp = _git(["rev-parse", "--show-toplevel"], cwd=path, timeout=10)
        return Path(cp.stdout.strip())
    except RuntimeError as e:
        if re.search(r"not a git repository", str(e), re.IGNORECASE):
            return None
        raise


def _git_remote_url(path: Path) -> str:
    """Return the origin remote URL, or '' if the repo has no origin remote.

    Any other git failure (missing binary, corruption, timeout) raises — collapsing
    it to '' would misreport a failing clone as 'no origin' and silently skip the
    remote-mismatch check that protects the index.
    """
    try:
        cp = _git(["remote", "get-url", "origin"], cwd=path, timeout=10)
        return cp.stdout.strip()
    except RuntimeError as e:
        if "No such remote 'origin'" in str(e):
            return ""
        raise


def _git_is_clean(path: Path) -> tuple[bool, list[str]]:
    """(is_clean, list_of_dirty_files). A clean tree has no modified or untracked files."""
    cp = _git(["status", "--porcelain"], cwd=path, timeout=10)
    if not cp.stdout.strip():
        return True, []
    # Parse dirty file paths: `git status --porcelain` gives "XY filename". splitlines()
    # + slice, not strip+split: an unstaged edit is " M file" — strip() would eat the
    # leading status space and shift the slice one column left, and split() breaks on
    # filenames containing spaces.
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
    except RuntimeError as e:
        if "git executable not found" in str(e):
            raise  # missing git is not 'ref not found'
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
    branch/tag; with ref=None or a pinned SHA, clones the remote's default branch
    (git rejects `--branch HEAD`, and `git clone --branch <sha>` fails — the pinned
    SHA is checked out afterwards by _git_checkout's detached path). Raises RuntimeError."""
    t = timeout if timeout is not None else _git_timeout()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"could not create {dest.parent}: {e}") from None
    args = ["clone", url, str(dest)]
    if ref:
        args[1:1] = ["--branch", ref]
    try:
        _git(args, cwd=dest.parent, timeout=t)
    except RuntimeError as e:
        # A hex-shaped ref might be a branch named like a SHA (git treats it as a
        # branch name — the --branch form above already succeeded for those) or a
        # true pinned SHA, which git rejects in --branch (its error: "Remote branch
        # <sha> not found"). Only THAT failure retries — a bad URL or network error
        # must raise immediately, not pay for a second full clone.
        if not (ref and _is_sha(ref)) or "Remote branch" not in str(e):
            raise
        args[1:3] = []                       # drop --branch <ref>, keep url + dest
        _git(args, cwd=dest.parent, timeout=t)


def _git_checkout(path: Path, ref: str) -> None:
    """Move the working tree to `ref`. For branches: reset to match origin/<ref>.
    For tags/SHAs: direct checkout, with one retry that fetches the tag by explicit
    refspec (a plain fetch cannot materialize a tag whose commit is unreachable from
    any branch). Raises RuntimeError on failure.

    Managed clones only — linked clones are read-only (ADR 0013). The caller
    (_ensure_git_repo_ready) has already fetched, so origin/<ref> is current; the
    branch path must not fetch again (that doubled network traffic on every refresh).
    """
    # Determine if `ref` is a branch by checking if origin/<ref> exists
    is_branch = False
    try:
        _git(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        is_branch = True
    except RuntimeError as e:
        if "git executable not found" in str(e):
            raise  # missing git is not 'not a branch'

    if is_branch:
        # Reset to match the remote the caller just fetched
        _git(["checkout", "-B", ref, f"origin/{ref}"], cwd=path,
             timeout=_git_timeout())
    else:
        # Tag or SHA: direct checkout. A plain `git fetch origin` does not bring a
        # tag whose commit is unreachable from a fetched branch, so the retry must
        # fetch the tag by explicit refspec — and only when the failure was the
        # ref missing locally (a dirty tree would fail identically after the fetch).
        try:
            _git(["checkout", ref], cwd=path, timeout=_git_timeout())
        except RuntimeError as e:
            if "did not match any file(s) known to git" not in str(e):
                raise
            try:
                _git(["fetch", "origin", f"refs/tags/{ref}:refs/tags/{ref}"],
                     cwd=path, timeout=_git_timeout())
            except RuntimeError:
                raise e from None           # the ref really doesn't exist — original error
            _git(["checkout", ref], cwd=path, timeout=_git_timeout())


def _is_sha(ref: str) -> bool:
    """True for a hex-shaped ref (full or short SHA-1 form). Hex shape alone does not
    make a pin — a branch named 'deadbeef' is a branch — that decision is
    _is_sha_pin's. A pinned SHA puts the checkout in git's detached-HEAD state:
    staleness undefined (ADR 0013)."""
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", ref))


def _is_sha_pin(path: Path, ref: str) -> bool:
    """True if `ref` is a pinned SHA (hex-shaped AND not a branch). A branch named
    'deadbeef' is a normal branch to git, not a pin — only a ref that does not
    resolve as any branch is treated as detached."""
    return _is_sha(ref) and not _git_is_branch(path, ref)


def _git_is_branch(path: Path, ref: str) -> bool:
    """True if `ref` resolves as a branch (origin-tracking or local)."""
    try:
        _git(["rev-parse", "--verify", f"origin/{ref}"], cwd=path, timeout=10)
        return True
    except RuntimeError as e:
        if "git executable not found" in str(e):
            raise
    try:
        _git(["rev-parse", "--verify", f"refs/heads/{ref}"], cwd=path, timeout=10)
        return True
    except RuntimeError:
        return False


def _git_is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is an ancestor of `descendant` (both must resolve locally).

    git exits 1 for the legitimate 'not an ancestor' answer and 128 for errors; only
    the exit-1 case returns False, so a git failure raises and the caller degrades
    to 'unknown' instead of reporting a wrong 'diverged'."""
    cp = _git(["merge-base", "--is-ancestor", ancestor, descendant],
              cwd=path, timeout=10, allow_exit_codes=(1,))
    return cp.returncode == 0


def _linked_git_status(repo_path: Path, src: Source, indexed: str) -> str:
    """git_status for a linked clone, in git's own terms — read-only: compares the
    indexed commit against the LOCAL origin/<ref> (origin first, local fallback for
    tags/SHAs/local-only branches — the clone's own knowledge; rtfm never fetches a
    linked clone, ADR 0013). A pinned SHA puts the checkout in git's detached-HEAD
    state — staleness undefined. An unresolvable ref means 'unknown' — check the ref
    spelling in the manifest."""
    try:
        ref = src.ref or _default_branch(repo_path)
        if _is_sha_pin(repo_path, ref):
            return "detached"
        origin_sha = _git_resolve_ref(repo_path, ref)   # origin/<ref> first, local fallback
    except RuntimeError:
        return "unknown"
    if indexed == origin_sha:
        return "up to date"
    if _git_is_ancestor(repo_path, origin_sha, indexed):
        return "ahead"
    if _git_is_ancestor(repo_path, indexed, origin_sha):
        return "behind"
    return "diverged"


def _managed_git_status(repo_path: Path, src: Source, indexed: str) -> str:
    """git_status for a managed clone: fetch (rtfm owns the clone), then compare the
    indexed commit to origin/<ref> (origin first, local fallback). The reset keeps
    HEAD at origin, so 'ahead' and 'diverged' are unreachable here. A pinned SHA
    needs no fetch — the pin never moves — and reports git's detached-HEAD state."""
    try:
        ref = src.ref or _default_branch(repo_path)
        if _is_sha_pin(repo_path, ref):
            return "detached"
        _git_fetch(repo_path)
        current = _git_resolve_ref(repo_path, ref)
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
            git_commit_date TEXT NOT NULL
        );
        CREATE TABLE web_meta (
            source     TEXT PRIMARY KEY,
            url        TEXT NOT NULL,
            version    TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            page_count INTEGER NOT NULL,
            total_pages INTEGER NOT NULL,
            lastmod    TEXT,
            status     TEXT NOT NULL,
            error      TEXT
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
    elif ext == ".html":
        rows = _html_rows(path)
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


_BLOCK_TAGS = {"p", "div", "li", "tr", "ul", "ol", "table", "section", "dt", "dd"}


class _MainExtractor(html.parser.HTMLParser):
    """Extract (title, headings, text_lines) from the div[role="main"] region of an
    RTD-style page. Everything outside that region (nav, footer, search, breadcrumbs)
    is chrome and never emitted. Code inside <pre> stays verbatim. <script>/<style>
    are dropped even inside main. Deterministic: same input, same output — read-tool
    locators (line numbers in the extracted text) depend on it."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.headings: list[str] = []
        self._lines: list[str] = []
        self._buf: list[str] = []
        self._main_depth = -1      # -1 outside main; >= 0 inside
        self._heading_tag = ""     # "h1".."h3" while inside a heading element
        self._pre = False          # inside <pre>: text verbatim, block-newline on </pre>
        self._skip = 0             # depth inside script/style

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs = dict(attrs)
        if self._main_depth < 0:
            if tag == "div" and attrs.get("role") == "main":
                self._main_depth = 0
            return
        self._main_depth += 1
        if tag in ("script", "style"):
            self._skip += 1
        elif self._skip == 0:
            if tag == "pre":
                self._emit_block()
                self._pre = True
            elif tag in ("h1", "h2", "h3"):
                self._emit_block()
                self._heading_tag = tag
            elif tag == "br":
                self._emit_block()
            elif tag in _BLOCK_TAGS:
                self._emit_block()

    def handle_endtag(self, tag: str) -> None:
        if self._main_depth < 0:
            return
        if self._skip > 0:
            if tag in ("script", "style"):
                self._skip -= 1
        elif self._pre:
            if tag == "pre":
                self._emit_block()
                self._pre = False
        elif self._heading_tag and tag == self._heading_tag:
            self._heading_tag = ""
        self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._main_depth < 0 or self._skip > 0:
            return
        if self._heading_tag:
            text = " ".join(data.split())
            if text:
                if self._heading_tag == "h1" and not self.title:
                    self.title = text
                self.headings.append(text)
        self._buf.append(data)

    def _emit_block(self) -> None:
        line = "".join(self._buf)
        self._buf = []
        if self._pre:
            self._lines.extend(ln.strip() for ln in line.splitlines())
        elif line.strip():
            self._lines.append(" ".join(line.split()))

    def result(self) -> tuple[str, str, list[str]]:
        self._emit_block()
        return self.title, "\n".join(self.headings), self._lines


def _html_to_text(html: str) -> tuple[str, str, list[str]]:
    """(title, headings, text_lines) from a page's div[role="main"]. ("", "", []) when
    the page has no main region (the trust-the-URL failure signal: NOT_READTHEDOCS)."""
    p = _MainExtractor()
    p.feed(html)
    p.close()
    return p.result()


def _html_doc_signal(path: Path) -> tuple[str, str]:
    title, headings, _ = _html_to_text(path.read_text(errors="replace"))
    return title, headings


def _html_rows(path: Path) -> list[tuple[str, str, str]]:
    """Line-chunked rows for an .html file. Locator line numbers index the EXTRACTED
    text (read re-extracts, so hit locators and read() stay consistent)."""
    _, _, lines = _html_to_text(path.read_text(errors="replace"))
    if not lines:
        return []
    rows: list[tuple[str, str, str]] = []
    for i in range(0, len(lines), CHUNK_LINES):
        chunk = "\n".join(lines[i:i + CHUNK_LINES])
        rows.append(("line", str(i + 1), chunk))
    return rows


def _doc_signal_for_file(path: Path) -> tuple[str, str]:
    """Doc-level (title, headings) for ranking. Best-effort — a failure yields ("", "") so the
    document still ranks on body text and signal extraction never blocks body extraction. The
    failure is logged (not silent): a broken/absent pymupdf would otherwise strip title/heading
    ranking corpus-wide with nothing to grep."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _pdf_doc_signal(path)
        if ext == ".html":
            return _html_doc_signal(path)
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


def _normalize_remote(url: str) -> str:
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


def _dirty_error(repo_path: Path) -> str | None:
    """Classified DIRTY_TREE error if the working tree has changes, else None."""
    clean, dirty_files = _git_is_clean(repo_path)
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
            root = _git_repo_root(repo_path)
        except RuntimeError as e:
            return repo_path, _git_error_class(e)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} is not a git repository. "
                f"Recover: clone the repo or remove 'path' from the manifest.")
        if src.url:
            try:
                remote = _git_remote_url(root)
            except RuntimeError as e:
                return repo_path, _git_error_class(e)
            if remote and _normalize_remote(remote) != _normalize_remote(src.url):
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
    repo_path = _managed_repo_path(src.name)
    if not (repo_path / ".git").exists():
        try:
            _git_clone(src.url, src.ref, repo_path)   # ref None → default branch
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
            root = _git_repo_root(repo_path)
        except RuntimeError as e:
            return repo_path, _git_error_class(e)
        if root is None:
            return repo_path, (
                f"ERROR:NOT_GIT_REPO: {repo_path} exists but is not a git repository. "
                f"Recover: remove {repo_path} and reindex.")
        try:
            remote = _git_remote_url(repo_path)
        except RuntimeError as e:
            return repo_path, _git_error_class(e)
        if remote and src.url and _normalize_remote(remote) != _normalize_remote(src.url):
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
        _git_fetch(repo_path)
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
        _git_checkout(repo_path, ref)
    except RuntimeError as e:
        return repo_path, (
            f"ERROR:CHECKOUT_FAILED: ref '{ref}' — {e}. "
            f"Recover: verify the ref exists on the remote and try again.")

    return repo_path, None


def _index_files(conn: sqlite3.Connection, source_name: str, root: Path) -> dict:
    """Scan `root` for supported files and sync the index for `source_name`: hash new or
    mtime-changed files, purge vanished ones, extract new contents, GC orphaned rows.
    Returns a summary of the shared counters. Shared by the dir and git_repo reindex
    paths so the two can never drift apart."""
    summary = {"files_seen": 0, "unique_contents": 0, "newly_extracted": 0,
               "extraction_skips": 0, "purged": 0, "errors": 0}
    files = iter_source_files(Source(name=source_name, type="dir", path=root))
    summary["files_seen"] = len(files)

    existing = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT relpath, sha256, mtime FROM locations WHERE source=?", (source_name,))}

    present: dict[str, tuple[str, float]] = {}
    for f in files:
        rel = str(f.relative_to(root))
        mtime = f.stat().st_mtime
        prev = existing.get(rel)
        if prev and prev[1] == mtime:
            sha = prev[0]                                   # mtime match: reuse stored sha
        else:
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
        present[rel] = (sha, mtime)

    vanished = set(existing) - set(present)
    for rel in vanished:
        conn.execute("DELETE FROM locations WHERE source=? AND relpath=?", (source_name, rel))
    summary["purged"] = len(vanished)

    for rel, (sha, mtime) in present.items():
        conn.execute(
            "INSERT INTO locations(source, relpath, sha256, mtime) VALUES(?,?,?,?) "
            "ON CONFLICT(source, relpath) DO UPDATE SET "
            "sha256=excluded.sha256, mtime=excluded.mtime",
            (source_name, rel, sha, mtime))
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
            jobs[sha] = str(root / rel)

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


def _reindex_git_repo(conn: sqlite3.Connection, src: Source) -> dict:
    """Rebuild a git_repo source."""
    repo_path, error = _ensure_git_repo_ready(src)
    if error:
        return {"source": src.name, "error": error}

    # Record commit before indexing
    try:
        commit = _git_current_commit(repo_path)
        commit_date = _git_commit_date(repo_path, "HEAD")
    except RuntimeError as e:
        if re.search(r"ambiguous argument 'HEAD'|Needed a single revision", str(e)):
            return {"source": src.name,
                    "error": (f"ERROR:EMPTY_REPO: {repo_path} has no commits yet. "
                              f"Recover: commit something first, or point 'ref' at an "
                              f"existing commit.")}
        return {"source": src.name, "error": _git_error_class(e)}
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date) VALUES(?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "git_commit=excluded.git_commit, git_commit_date=excluded.git_commit_date",
        (src.name, commit, commit_date))
    conn.commit()
    _staleness_cache.pop((src.name, src.ref), None)  # a fresh index makes the cached verdict stale

    # Now run the file-level indexing (shared core with dir sources)
    summary = {"source": src.name, "commit": commit, "commit_date": commit_date}
    if src.path is not None and src.ref:
        try:
            _git_resolve_ref(repo_path, src.ref)
        except RuntimeError:
            # Read-only mode cannot validate the ref by checking it out — surface
            # the typo where the work happens, not only in list_sources' 'unknown'.
            summary["warning"] = (
                f"ref '{src.ref}' does not resolve in the clone — check the ref "
                f"spelling in the manifest, or the ref may not be fetched into the "
                f"clone yet (run 'git fetch --tags' there); list_sources reports "
                f"'unknown'.")
    summary.update(_index_files(conn, src.name, repo_path))
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
    if src.type == "web":
        return _reindex_web(conn, src)
    summary = {"source": src.name, "files_seen": 0, "unique_contents": 0,
               "newly_extracted": 0, "extraction_skips": 0, "purged": 0, "errors": 0}
    if src.path is None or not src.path.exists():
        return summary
    summary.update(_index_files(conn, src.name, src.path))
    return summary


STALENESS_TTL = 30  # seconds: git_repo verdicts are memoized in-process for this long


def _stale_delta(conn: sqlite3.Connection, src: Source) -> tuple[int, bool, bool]:
    """Cheap staleness check. Dispatches on source type.
    Returns (changed, stale, cached):
      changed  count of new/changed items (files for dir, always 0 for git_repo)
      stale    whether the index differs from reality
      cached   True when a git_repo verdict came from the memo (<= STALENESS_TTL old)

    git_repo verdicts cost git subprocesses (and a fetch for managed sources) and
    feed the auto-reindex attempt and its failure warning — both are memoized per
    source+ref for STALENESS_TTL seconds (ADR 0013): the check runs at most every
    30 s, and search re-attempts a stale source at most every 30 s, so a
    persistently broken or dirty source warns on the first query of each window,
    not on every query.
    """
    if src.type == "git_repo":
        now = time.monotonic()
        hit = _staleness_cache.get((src.name, src.ref))
        if hit and now - hit[0] < STALENESS_TTL:
            return 0, hit[1], True
        _, stale = _stale_delta_git_repo(conn, src)
        _staleness_cache[(src.name, src.ref)] = (now, stale)
        return 0, stale, False
    # dir sources: stat + (relpath, mtime) compare — no hashing or extraction.
    # `changed` counts files that would need fresh extraction and bounds the cost of an
    # inline auto-reindex (see _auto_reindex_max); `stale` also catches files that vanished
    # on disk (a reindex purges them essentially for free, so they don't inflate `changed`).
    if src.path is None or not src.path.exists():
        return 0, False, False
    indexed = {r[0]: r[1] for r in conn.execute(
        "SELECT relpath, mtime FROM locations WHERE source=?", (src.name,))}
    on_disk = {str(f.relative_to(src.path)): f.stat().st_mtime for f in iter_source_files(src)}
    changed = sum(1 for rel, mtime in on_disk.items() if indexed.get(rel) != mtime)
    stale = changed > 0 or set(indexed) != set(on_disk)   # latter catches vanished files
    return changed, stale, False


_staleness_cache: dict[tuple[str, str | None], tuple[float, bool]] = {}


def _stale_delta_git_repo(conn: sqlite3.Connection, src: Source) -> tuple[int, bool]:
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
            "SELECT git_commit FROM source_meta WHERE source=?", (src.name,)).fetchone()
        if row is None:
            return 0, True  # never indexed — always stale
        repo_path = src.path if src.path is not None else _managed_repo_path(src.name)
        if not repo_path.exists():
            return 0, True  # clone vanished
        ref = src.ref or _default_branch(repo_path)
        if src.path is not None:
            if _is_sha_pin(repo_path, ref):
                return 0, False  # linked pin: never auto-reindex (ADR 0013)
            head_moved = row[0] != _git_current_commit(repo_path)
            if head_moved:
                return 0, True
            clean, _ = _git_is_clean(repo_path)
            return 0, not clean  # dirty tree: reindex refuses loudly, search warns
        _git_fetch(repo_path)
        current = _git_resolve_ref(repo_path, ref)
    except RuntimeError:
        return 0, True  # graceful degradation: git failure means stale (will warn)
    return 0, (row[0] != current)


def _default_branch(path: Path) -> str:
    """Return the remote's default branch name as the clone knows it (origin's HEAD
    symbolic ref), 'main' as fallback. Raises when the repo has no origin at all
    (callers classify NO_REMOTE). Purely local — `git remote show origin` does a
    network round-trip, which would stall list_sources offline and touch the user's
    remote in linked mode."""
    if not _git_remote_url(path):
        raise RuntimeError(f"{path} has no 'origin' remote")
    try:
        cp = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=path, timeout=10)
        return cp.stdout.strip().removeprefix("refs/remotes/origin/")
    except RuntimeError:
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
    type: str                       # "dir", "git_repo", or "web"
    path: Path | None = None
    url: str | None = None
    ref: str | None = None          # git refspec (branch, tag, SHA); None for dir
    flavor: str | None = None       # hosting-family parsing strategy ("readthedocs"); web only
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
        flavor=t.get("flavor"),                  # None for dir/git_repo
        mutable=bool(t.get("mutable", False)),
    )


def _validate_source(s: Source) -> str | None:
    """Loud warning if a source is misconfigured, else None. A dir source needs an existing,
    readable directory; a `path`-less dir source is unusable (the caller drops it). A git_repo
    source needs a url; in linked mode (`path` set) the path must be a git working tree whose
    origin remote matches the declared url. A web source needs a url and a known flavor. The
    point is that one bad entry never silently disappears and never breaks the others. The URL
    itself is trusted as given — no shape gate (ADR 0014): a non-RTD URL fails at index time
    with NOT_READTHEDOCS, never at load."""
    if s.type == "web":
        if not s.url:
            return (f"!!! INVALID SOURCE '{s.name}' !!! web source has no 'url' — "
                    f"the index page URL is required. Recover: add url = \"...\" in "
                    f"{manifest_path()}.")
        if not s.flavor:
            return (f"!!! INVALID SOURCE '{s.name}' !!! web source has no 'flavor' — "
                    f"expected 'readthedocs'. Recover: add flavor = \"readthedocs\" in "
                    f"{manifest_path()}.")
        if s.flavor not in ("readthedocs",):
            return (f"!!! INVALID SOURCE '{s.name}' !!! unknown flavor '{s.flavor}' — "
                    f"expected 'readthedocs'. Recover: fix 'flavor' in {manifest_path()}.")
        return None
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
            try:
                root = _git_repo_root(s.path)
            except RuntimeError as e:
                if "git executable not found" in str(e):
                    return (f"!!! GIT MISSING '{s.name}' !!! git executable not found. "
                            f"Recover: install git, or remove the source.")
                return (f"!!! GIT FAILED '{s.name}' !!! {e} — "
                        f"Recover: fix the repo's git state (permissions, ownership, "
                        f"corruption) and try again.")
            if root is None:
                return (f"!!! NOT A GIT REPO '{s.name}' !!! {s.path} is not a git repository. "
                        f"Recover: clone the repo to that path, or remove 'path' to let rtfm "
                        f"manage the clone.")
            try:
                remote = _git_remote_url(root)
            except RuntimeError as e:
                if "git executable not found" in str(e):
                    return (f"!!! GIT MISSING '{s.name}' !!! git executable not found. "
                            f"Recover: install git, or remove the source.")
                return (f"!!! GIT FAILED '{s.name}' !!! {e} — "
                        f"Recover: fix the repo's git state (permissions, ownership, "
                        f"corruption) and try again.")
            if not remote:
                return (f"!!! NO REMOTE '{s.name}' !!! {s.path} has no 'origin' remote. "
                        f"Recover: add a remote with 'git remote add origin <url>'.")
            if _normalize_remote(remote) != _normalize_remote(s.url):
                return (f"!!! REMOTE URL MISMATCH '{s.name}' !!! {s.path} has origin "
                        f"'{remote}', but manifest declares '{s.url}'. "
                        f"Recover: fix 'url' in {manifest_path()} or update the clone's "
                        f"origin with 'git remote set-url origin {s.url}'.")
        return None
    if s.type != "dir":
        # An unknown type is a config error like any other — silent skipping made a
        # typo'd source invisible AND made sources_searched claim it was searched.
        return (f"!!! INVALID SOURCE '{s.name}' !!! unknown type '{s.type}' — "
                f"expected 'dir', 'git_repo', or 'web'. Recover: fix 'type' in "
                f"{manifest_path()}.")
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
    warning (ADR 0006). Misconfigured sources are validated: a `path`-less dir source or a
    url-less git_repo source is dropped (loudly),
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
            if (s.type == "dir" and s.path is None) or (s.type == "git_repo" and not s.url) \
                    or (s.type == "web" and not s.url):
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
    """Read a page range (pdf) or line range (text) from a file in a source.

    A path-less git_repo source reads from its managed clone
    (~/.rtfm/repos/<name>/); a path-less dir source has nothing to read."""
    if src.path is not None:
        root = src.path
    elif src.type == "git_repo":
        root = _managed_repo_path(src.name)
        if not root.exists():
            return (f"!!! ERROR !!! source '{src.name}' has no clone yet — "
                    f"run reindex('{src.name}') first.")
    elif src.type == "web":
        root = _web_cache_path(src.name)
        if not root.exists():
            return (f"!!! ERROR !!! source '{src.name}' has no cache yet — "
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
    if path.suffix.lower() == ".html":
        # Locator line numbers index the EXTRACTED text (not the raw markup) — read
        # must re-extract so hits and reads stay consistent (ADR 0014).
        _, _, lines = _html_to_text(path.read_text(errors="replace"))
        e = end if end is not None else len(lines)
        return "\n".join(lines[max(0, start - 1):e])
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
            changed, stale, cached = _stale_delta(conn, s)
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
            elif cached:
                # The verdict came from the memo (<= STALENESS_TTL old): the reindex
                # attempt and its failure warning run once per window, not on every
                # query — a persistently broken or dirty source must not block or
                # spam every search (ADR 0013).
                continue
            else:  # git_repo — always auto-reindex, budget doesn't apply
                result = reindex_source(conn, s)
                # Multiple sources on one clone (or one broken remote) would
                # otherwise repeat the identical warning once per source.
                key = (s.path if s.path is not None
                       else str(_managed_repo_path(s.name)))
                message = None
                if isinstance(result, dict) and result.get("error"):
                    message = (f"!!! AUTO-REINDEX FAILED '{s.name}' !!! {result['error']} — "
                               f"searching previously indexed content only. "
                               f"Recover: run reindex('{s.name}').")
                elif isinstance(result, dict) and result.get("warning"):
                    # A non-blocking warning (e.g. a linked ref that doesn't
                    # resolve) — surface it when a refresh does run.
                    message = f"!!! SOURCE WARNING '{s.name}' !!! {result['warning']}"
                if message:
                    if key in warned_repo_paths:
                        continue
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
            for key in [k for k in _staleness_cache if k[0] == name]:
                _staleness_cache.pop(key, None)
        if dropped:
            conn.commit()
    resp: dict = {"reindexed": [reindex_source(conn, s) for s in targets]}
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


if __name__ == "__main__":
    mcp.run()
