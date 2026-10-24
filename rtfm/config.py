"""Paths, extension sets, and schema constants for the rtfm core."""

from __future__ import annotations

import os
from pathlib import Path

# The markup routine's extension set — a HANDLING concern (ATX/setext heading
# extraction for doc-level ranking, ADR 0012), never a selection default. TEXT_EXTS
# did both jobs under one name, which is how a .bzl file came to be selected and then
# extracted to zero rows (ADR 0014).
MARKUP_EXTS = frozenset({".txt", ".md", ".mdx", ".rst", ".rest"})

# Step 2's default deny set, consulted in ext_blocklist mode only. Growing this list
# removes garbage from an index; growing an allowlist would silently add content to every
# source that never asked for it, which is why there is no default allowlist (ADR 0014).
DEFAULT_EXT_BLOCKLIST = frozenset({
    # images and assets
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    ".svg", ".svgz", ".psd", ".ai", ".eps",
    # fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".whl",
    # audio and video
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".avi", ".mov", ".mkv", ".webm",
    # compiled and linkable artifacts
    ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".lib", ".pyc", ".pyo", ".class",
    ".wasm", ".node",
    # binary data and containers
    ".db", ".sqlite", ".sqlite3", ".bin", ".dat", ".pickle", ".pkl", ".npy", ".npz",
    ".parquet", ".avro", ".pb",
    # office binaries (zip containers; never valid UTF-8)
    ".doc", ".xls", ".ppt", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
})
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


def managed_repo_path(name: str) -> Path:
    """Path rtfm uses when a git_repo source omits `path`."""
    return corpus_home() / "repos" / name


def git_timeout() -> int:
    """Timeout in seconds for git subprocess calls. 0 = no timeout."""
    env = os.environ.get("RTFM_GIT_TIMEOUT")
    if env and env.isdigit():
        return int(env)
    return 60
