"""The source manifest: parsing, normalization, and validation.

A bad entry is reported loudly and never breaks the others (ADR 0006).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import git_ops
from .config import (
    corpus_home,
    default_source_dir,
    index_db_path,
    manifest_path,
)


@dataclass
class Source:
    name: str
    type: str                       # "dir" or "git_repo"
    path: Path | None = None
    url: str | None = None
    ref: str | None = None          # git refspec (branch, tag, SHA); None for dir
    mutable: bool = False
    # --- scope (ADR 0014). Normalized at parse time; never re-normalized downstream.
    paths: tuple[str, ...] = ()             # directory prefixes to include; () = whole tree
    exclude_paths: tuple[str, ...] = ()     # prefixes to exclude; exclusion wins
    ext_allowlist: frozenset[str] | None = None   # exactly one of these two is set
    ext_blocklist: frozenset[str] | None = None


_BOOTSTRAP_MANIFEST = '''\
# rtfm source manifest. See manifest.example.toml in the repo for all options.
# Each [[source]] is one place rtfm indexes, in place.

[[source]]
name          = "default"   # the zero-config drop-dir; the only mutable source by default
type          = "dir"
path          = "{default}"
mutable       = true
ext_blocklist = []          # index everything except rtfm's known binary and asset types

# Example: a git-tracked doc repo. rtfm clones and tracks the ref automatically
# when `path` is omitted (managed mode), or links to an existing clone when
# `path` is provided. `ref` defaults to the remote's HEAD branch.
# [[source]]
# name    = "specs"
# type    = "git_repo"
# url     = "https://github.com/org/specs.git"
# ref     = "main"
# ext_allowlist = [".md"]   # exactly one of ext_allowlist/ext_blocklist is required
'''


def ensure_bootstrap() -> None:
    home = corpus_home()
    home.mkdir(parents=True, exist_ok=True)
    default_source_dir().mkdir(parents=True, exist_ok=True)
    index_db_path().parent.mkdir(parents=True, exist_ok=True)
    mp = manifest_path()
    if not mp.exists():
        mp.write_text(_BOOTSTRAP_MANIFEST.format(default=default_source_dir()))


_GLOB_CHARS = ("*", "?", "[")


def normalize_paths(entries) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split `paths` into (include, exclude) prefixes, normalized.

    A bare entry includes a prefix, a `!` entry excludes one (ADR 0014). Both halves are
    stripped of leading "./" and surrounding "/", deduped and sorted, so two manifests that
    mean the same thing produce the same `config_scope` string and neither reads as stale
    against the other.
    """
    include, exclude = set(), set()
    for raw in entries or ():
        text = str(raw).strip()
        target = exclude if text.startswith("!") else include
        text = text.lstrip("!").strip()
        if text.startswith("./"):
            text = text[2:]
        text = text.strip("/")
        if text:
            target.add(text)
    return tuple(sorted(include)), tuple(sorted(exclude))


def normalize_exts(entries) -> frozenset[str]:
    """Lowercase, dot-prefixed extensions. "MD", ".Md" and "md" are one extension."""
    out = set()
    for raw in entries or ():
        text = str(raw).strip().lower()
        if text and not text.startswith("."):
            text = "." + text
        if text:
            out.add(text)
    return frozenset(out)


def source_from_table(t: dict) -> Source:
    path = t.get("path")
    url = t.get("url")
    name = t.get("name")
    if not name:
        basis = path or url or "source"
        name = Path(str(basis)).name or "source"
    include, exclude = normalize_paths(t.get("paths"))
    return Source(
        name=name,
        type=t.get("type", "dir"),
        path=Path(path).expanduser() if path else None,
        url=url,
        ref=t.get("ref"),                        # None if absent → default to remote HEAD later
        mutable=bool(t.get("mutable", False)),
        paths=include,
        exclude_paths=exclude,
        ext_allowlist=(normalize_exts(t["ext_allowlist"])
                       if "ext_allowlist" in t else None),
        ext_blocklist=(normalize_exts(t["ext_blocklist"])
                       if "ext_blocklist" in t else None),
    )


def validate_source(s: Source) -> str | None:
    """Loud warning if a source is misconfigured, else None. A dir source needs an existing,
    readable directory; a `path`-less dir source is unusable (the caller drops it). A git_repo
    source needs a url; in linked mode (`path` set) the path must be a git working tree whose
    origin remote matches the declared url. The point is that one bad entry never silently
    disappears and never breaks the others."""
    if s.ext_allowlist is not None and s.ext_blocklist is not None:
        return (f"!!! INVALID SOURCE '{s.name}' !!! declares both 'ext_allowlist' and "
                f"'ext_blocklist' — exactly one is required, because they answer the same "
                f"question in opposite directions. Recover: delete one in {manifest_path()}.")
    if s.ext_allowlist is None and s.ext_blocklist is None:
        return (f"!!! INVALID SOURCE '{s.name}' !!! declares neither 'ext_allowlist' nor "
                f"'ext_blocklist' — rtfm does not guess what to index. Recover: add "
                f"ext_allowlist = [\".md\", \".pdf\"] to index only those types, or "
                f"ext_blocklist = [] to index everything except rtfm's known binary and "
                f"asset types, in {manifest_path()}.")
    bad = [p for p in (*s.paths, *s.exclude_paths)
           if any(c in p for c in _GLOB_CHARS)]
    if bad:
        return (f"!!! INVALID SOURCE '{s.name}' !!! 'paths' takes directory prefixes, not "
                f"globs: {', '.join(sorted(bad))}. A literal 'docs/**' directory does not "
                f"exist, so the entry would index nothing while looking correct. Recover: "
                f"write the prefix itself (e.g. \"docs\") in {manifest_path()}.")
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
                root = git_ops.repo_root(s.path)
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
                remote = git_ops.remote_url(root)
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
            if git_ops.normalize_remote(remote) != git_ops.normalize_remote(s.url):
                return (f"!!! REMOTE URL MISMATCH '{s.name}' !!! {s.path} has origin "
                        f"'{remote}', but manifest declares '{s.url}'. "
                        f"Recover: fix 'url' in {manifest_path()} or update the clone's "
                        f"origin with 'git remote set-url origin {s.url}'.")
        return None
    if s.type != "dir":
        # An unknown type is a config error like any other — silent skipping made a
        # typo'd source invisible AND made sources_searched claim it was searched.
        return (f"!!! INVALID SOURCE '{s.name}' !!! unknown type '{s.type}' — "
                f"expected 'dir' or 'git_repo'. Recover: fix 'type' in "
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
    ensure_bootstrap()
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
        s = source_from_table(t)
        if s.name in seen:
            warnings.append(
                f"!!! DUPLICATE SOURCE NAME '{s.name}' !!! "
                f"keeping {seen[s.name].path or seen[s.name].url}, REFUSING "
                f"{s.path or s.url}. Source names must be unique. "
                f"Recover: rename one in {manifest_path()} "
                f"(version-stamp them, e.g. '{s.name}-2025.06')."
            )
            continue
        warn = validate_source(s)
        if warn:
            warnings.append(warn)
            if ((s.type == "dir" and s.path is None)
                    or (s.type == "git_repo" and not s.url)
                    or (s.ext_allowlist is None) == (s.ext_blocklist is None)
                    or any(c in p for p in (*s.paths, *s.exclude_paths)
                           for c in _GLOB_CHARS)):
                continue                       # unusable — drop it (loudly, above)
        seen[s.name] = s
        sources.append(s)
    return sources, warnings
