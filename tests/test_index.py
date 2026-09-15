# tests/test_index.py
import os
import sqlite3
import subprocess

from conftest import make_git_repo

import rtfm_server as rtfm

# What _reindex_git_repo stores for a source with no declared narrowing. Tests
# that hand-craft a source_meta row must set it, or the scope check reads them
# as stale before any commit comparison happens.
_NO_SCOPE = rtfm._source_scope(rtfm.Source(name="x", type="git_repo"))


def test_schema_is_content_addressed(home):
    conn = rtfm.get_index_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rtfm.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contents", "locations", "source_meta"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0] == 0


def test_migration_drops_old_schema(home):
    p = rtfm.index_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(p)
    old.executescript(
        "CREATE TABLE doc_meta(source TEXT, relpath TEXT);"
        "CREATE VIRTUAL TABLE doc_fts USING fts5(text);"
    )
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    conn = rtfm.get_index_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rtfm.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "doc_meta" not in tables                        # legacy table dropped
    assert {"contents", "locations", "doc_fts", "source_meta"} <= tables  # current v4 schema
    cols = {r[1] for r in conn.execute("PRAGMA table_info(doc_fts)")}
    assert {"title", "headings"} <= cols                    # old doc_fts(text) rebuilt for v4
    # source_meta is empty but present
    assert conn.execute("SELECT COUNT(*) FROM source_meta").fetchone()[0] == 0


def test_reindex_dedups_identical_files(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("shared keyword body\n")
    (d / "b.md").write_text("shared keyword body\n")  # byte-identical
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d))
    assert summary["files_seen"] == 2
    assert summary["unique_contents"] == 1
    assert summary["newly_extracted"] == 1
    assert summary["extraction_skips"] == 1
    assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='docs'").fetchone()[0] == 2


def test_reindex_reuses_sha_on_unchanged_mtime(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("keyword body\n")
    src = rtfm.Source(name="docs", type="dir", path=d)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, src)
    again = rtfm.reindex_source(conn, src)  # nothing changed
    assert again["extraction_skips"] == 1 and again["newly_extracted"] == 0


def test_reindex_purges_and_gcs_deleted_files(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    f = d / "a.md"
    f.write_text("keyword\n")
    src = rtfm.Source(name="docs", type="dir", path=d)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, src)
    f.unlink()
    again = rtfm.reindex_source(conn, src)
    assert again["purged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='docs'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0  # orphan GC'd


def test_reindex_indexes_pdf_pages(home, sample_pdf, tmp_path):
    import shutil

    d = tmp_path / "docs"
    d.mkdir()
    shutil.copy(sample_pdf, d / "sample.pdf")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d))
    rows = conn.execute(
        "SELECT locator_kind, locator_value FROM content_fts ORDER BY locator_value"
    ).fetchall()
    assert [r[0] for r in rows] == ["page", "page"]
    assert rows[0][1] == "1"


def test_edit_in_place_reextracts_and_gcs_old_sha(home, tmp_path):
    """Rewriting a file in place: new sha extracted, old sha GC'd, search finds new content."""
    d = tmp_path / "docs"
    d.mkdir()
    f = d / "a.md"
    src = rtfm.Source(name="docs", type="dir", path=d)
    conn = rtfm.get_index_db()

    f.write_text("content X original body\n")
    rtfm.reindex_source(conn, src)
    sha_x = conn.execute("SELECT sha256 FROM locations WHERE relpath='a.md'").fetchone()[0]

    # Rewrite with different content and bump mtime so the fast-path re-hashes.
    f.write_text("content Y replacement body\n")
    new_mtime = f.stat().st_mtime + 1.0
    os.utime(f, (new_mtime, new_mtime))

    rtfm.reindex_source(conn, src)

    # Only one row in contents: the new sha, not the old one.
    assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 1
    sha_y = conn.execute("SELECT sha256 FROM locations WHERE relpath='a.md'").fetchone()[0]
    assert sha_y != sha_x

    # FTS finds the new content, not the old.
    hits = rtfm.search_index(conn, "replacement")
    assert hits and any("replacement" in h["snippet"] for h in hits)
    assert not rtfm.search_index(conn, "original")


def test_workers_respects_env(monkeypatch):
    monkeypatch.setenv("RTFM_WORKERS", "3")
    assert rtfm._workers() == 3
    monkeypatch.delenv("RTFM_WORKERS", raising=False)
    assert rtfm._workers() >= 1


def test_parallel_extraction_matches_serial(home, tmp_path, monkeypatch):
    d = tmp_path / "docs"
    d.mkdir()
    for i in range(4):
        (d / f"f{i}.md").write_text(f"unique body {i} keyword{i}\n")
    monkeypatch.setenv("RTFM_WORKERS", "2")  # force the pool path
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    assert summary["unique_contents"] == 4 and summary["newly_extracted"] == 4
    hits = rtfm.search_index(conn, "keyword2")
    assert hits and hits[0]["locations"][0]["relpath"] == "f2.md"


def test_failed_extraction_recorded_not_raised(home, tmp_path):
    """A file that all extractors reject is recorded with extracted_ok=0; reindex does not raise."""
    d = tmp_path / "docs"
    d.mkdir()
    bad = d / "bad.pdf"
    bad.write_bytes(b"\x00\x01\x02\x03 this is definitely not a pdf and has no pdf header")
    src = rtfm.Source(name="docs", type="dir", path=d)
    conn = rtfm.get_index_db()

    summary = rtfm.reindex_source(conn, src)  # must not raise

    assert summary["errors"] == 1
    assert summary["newly_extracted"] == 0
    row = conn.execute(
        "SELECT extracted_ok, error FROM contents WHERE sha256 IN "
        "(SELECT sha256 FROM locations WHERE relpath='bad.pdf')"
    ).fetchone()
    assert row is not None
    assert row[0] == 0
    assert row[1] is not None and len(row[1]) > 0


def test_failed_extraction_not_retried_on_unchanged_bytes(home, tmp_path):
    """A failed extraction is remembered; a second reindex with unchanged bytes does NOT retry it.

    Spec §3: "Failed extractions are recorded so they are not retried every run;
    a content is retried only if its bytes change (new sha)."
    """
    d = tmp_path / "docs"
    d.mkdir()
    bad = d / "bad.pdf"
    bad.write_bytes(b"\x00\x01\x02\x03 not a pdf")
    src = rtfm.Source(name="docs", type="dir", path=d)
    conn = rtfm.get_index_db()

    # First run: extraction is attempted and fails.
    first = rtfm.reindex_source(conn, src)
    assert first["errors"] == 1, f"expected 1 error on first run, got {first}"
    assert first["newly_extracted"] == 0

    # Confirm the failure is recorded in contents.
    sha = conn.execute(
        "SELECT sha256 FROM locations WHERE source='docs' AND relpath='bad.pdf'"
    ).fetchone()[0]
    row_after_first = conn.execute(
        "SELECT extracted_ok FROM contents WHERE sha256=?", (sha,)
    ).fetchone()
    assert row_after_first is not None and row_after_first[0] == 0

    # Second run: file bytes unchanged — must NOT re-attempt extraction.
    second = rtfm.reindex_source(conn, src)
    assert second["errors"] == 0, f"expected 0 errors on second run (no retry), got {second}"
    assert second["newly_extracted"] == 0
    # The bad file still counts as a skip (it was in `already`).
    assert second["extraction_skips"] == 1

    # The contents row is preserved with extracted_ok=0 (not removed or flipped).
    row_after_second = conn.execute(
        "SELECT extracted_ok FROM contents WHERE sha256=?", (sha,)
    ).fetchone()
    assert row_after_second is not None and row_after_second[0] == 0


def test_extraction_runs_in_current_process_not_forked(home, tmp_path, monkeypatch):
    """Regression guard for the FastMCP-server deadlock: parallel extraction must run in THIS
    process (a thread pool), never a forked child. A process pool deadlocks inside the server
    (forked workers inherit thread-held locks and hang). Force the pool path and assert every
    extraction ran under our PID. The old ProcessPoolExecutor fails this: a forked worker runs
    in a different PID and its appends never reach our list (and this closure spy isn't even
    picklable for a process pool)."""
    d = tmp_path / "docs"
    d.mkdir()
    for i in range(4):
        (d / f"f{i}.md").write_text(f"body {i} keyword{i}\n")
    monkeypatch.setenv("RTFM_WORKERS", "4")  # force the pool path (>1 worker, >1 job)

    seen_pids: list[int] = []
    real = rtfm._extract_rows

    def spy(path_str):
        seen_pids.append(os.getpid())
        return real(path_str)

    monkeypatch.setattr(rtfm, "_extract_rows", spy)
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))

    assert summary["newly_extracted"] == 4
    assert seen_pids, "extraction worker never ran in this process (out-of-process pool?)"
    assert all(pid == os.getpid() for pid in seen_pids)


def test_extraction_works_from_worker_thread_with_event_loop(home, tmp_path):
    """Mimic the FastMCP server runtime: a sync tool runs on a worker thread while an asyncio
    event loop runs in the background. Reindex must complete here, not wedge — the old
    fork-based pool could deadlock under exactly these conditions; a thread pool cannot."""
    import asyncio
    import threading

    d = tmp_path / "docs"
    d.mkdir()
    for i in range(4):
        (d / f"f{i}.md").write_text(f"body {i} keyword{i}\n")

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    result: dict = {}
    done = threading.Event()

    def run_reindex():
        conn = rtfm.get_index_db()  # sqlite connection owned by this thread
        result["summary"] = rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
        done.set()

    threading.Thread(target=run_reindex, daemon=True).start()
    completed = done.wait(timeout=30)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)  # run_forever returns once stopped; then free the loop
    loop.close()

    assert completed, "reindex did not complete from a worker thread within 30s (deadlock?)"
    assert result["summary"]["newly_extracted"] == 4


def test_reindex_indexes_rst_files(home, tmp_path):
    """`.rst` and `.rest` (reStructuredText) are plain text — they index like `.md`, with line
    locators. Unlocks Sphinx doc trees. Fails unless `.rst` is in the source's
    ext_allowlist (files skipped)."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "guide.rst").write_text(
        "Widget Protocol\n===============\n\nThe widget protocol defines flits and credits.\n")
    (d / "manual.rest").write_text(
        "Credit Scheme\n=============\n\nCredits gate the flit pipeline downstream.\n")
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    assert summary["files_seen"] == 2 and summary["newly_extracted"] == 2
    rows = conn.execute("SELECT locator_kind FROM content_fts").fetchall()
    assert rows and all(r[0] == "line" for r in rows)
    assert rtfm.search_index(conn, "widget protocol flits")
    assert rtfm.search_index(conn, "credits flit pipeline downstream")


def test_reindex_indexes_mdx_files(home, tmp_path):
    """`.mdx` is markdown carrying JSX components — the page format of Docusaurus- and
    Next.js-based doc sites, whose prose is otherwise unreachable. It indexes like `.md`,
    with line locators; component tags are inert text. Fails unless `.mdx` is in the
    source's ext_allowlist (the file is skipped, and the whole site indexes as nothing)."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "guide.mdx").write_text(
        "---\ntitle: Widget Protocol\n---\n\n"
        "import Tabs from '@theme/Tabs';\n\n"
        "# Widget Protocol\n\nThe widget protocol defines flits and credits.\n")
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    assert summary["files_seen"] == 1 and summary["newly_extracted"] == 1
    assert rtfm.search_index(conn, "widget protocol flits")


# --- _stale_delta for git_repo ---

def test_stale_delta_git_repo_current_is_not_stale(home, tmp_path, git_branch):
    """Exercises the linked not-stale path at a fresh clone (HEAD == origin/<ref>,
    clean tree); the HEAD-vs-origin distinction is pinned by
    test_stale_delta_git_repo_linked_head_moved_is_stale."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    commit = rtfm._git_current_commit(dest)
    commit_date = rtfm._git_commit_date(dest, "HEAD")
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", commit, commit_date, _NO_SCOPE))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                       url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is False
    assert changed == 0


def test_stale_delta_linked_dirty_is_stale(home, tmp_path, git_branch):
    """A linked git_repo with uncommitted edits at the indexed commit is stale —
    the reindex refusal then warns loudly on search instead of serving silently
    absent edits (ADR 0013: dirty = refuse)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    commit = rtfm._git_current_commit(dest)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", commit, "2025-01-01T00:00:00+00:00", _NO_SCOPE))
    conn.commit()
    (dest / "a.md").write_text("uncommitted v2\n")
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is True


def test_stale_delta_pinned_sha_never_stale(home, tmp_path, git_branch):
    """A pinned-SHA source is never stale once indexed — the pin never moves,
    staleness is undefined (ADR 0013)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    sha = rtfm._git_current_commit(dest)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", sha, "2025-01-01T00:00:00+00:00", _NO_SCOPE))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=sha)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is False


def test_stale_delta_git_repo_behind_is_stale(home, tmp_path, git_branch):
    """A managed git_repo whose indexed commit is behind origin/<ref> is stale —
    rtfm owns the clone, so the fetch-and-compare applies. (Linked clones are
    read-only: only the tree's HEAD or a dirty tree matters there — see
    test_stale_delta_git_repo_linked_head_moved_is_stale.)"""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    old_commit = rtfm._git_current_commit(dest)
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", branch], capture_output=True)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", old_commit, "2025-01-01T00:00:00+00:00", _NO_SCOPE))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is True


def test_stale_delta_git_repo_linked_head_moved_is_stale(home, tmp_path, git_branch):
    """A linked git_repo is stale when its HEAD moves (the user's checkout changed)
    — and NOT stale when only the remote moved (rtfm never fetches linked clones,
    so it cannot know; the tree is unchanged)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    old_commit = rtfm._git_current_commit(dest)

    # Remote moves on; the linked tree does not — not stale
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", branch], capture_output=True)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", old_commit, "2025-01-01T00:00:00+00:00", _NO_SCOPE))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is False  # remote moved, tree didn't — nothing to reindex

    # The USER refreshes their own clone (their fetch + checkout) — stale now
    subprocess.run(["git", "-C", str(dest), "fetch", "origin"], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"],
                    capture_output=True)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is True


def test_stale_delta_git_repo_no_source_meta_is_stale(home, tmp_path, git_branch):
    """A git_repo with no source_meta row is always stale (never been indexed)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                       url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta_git_repo(conn, src)
    assert stale is True


def test_default_branch_parses_remote_head(home, tmp_path, git_branch):
    """_default_branch reads the remote's HEAD branch, not a hardcoded guess."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    assert rtfm._default_branch(dest) == branch


# --- constants split: selection is declared, handling is code (ADR 0014) -----

def test_markup_exts_is_for_heading_parsing_not_selection():
    assert rtfm.MARKUP_EXTS == frozenset({".txt", ".md", ".mdx", ".rst", ".rest"})
    assert not hasattr(rtfm, "TEXT_EXTS")           # selection no longer lives in code


def test_default_ext_blocklist_covers_common_binaries():
    for ext in (".png", ".jpg", ".svg", ".gif", ".ico", ".woff", ".woff2",
                ".so", ".dylib", ".dll", ".zip", ".gz", ".jar", ".class", ".pyc"):
        assert ext in rtfm.DEFAULT_EXT_BLOCKLIST, ext
    assert ".md" not in rtfm.DEFAULT_EXT_BLOCKLIST
    assert ".pdf" not in rtfm.DEFAULT_EXT_BLOCKLIST  # pdf is indexable, not an asset


# --- declared scope applied at selection (ADR 0014) --------------------------

def _tree(base):
    for rel in ["docs/a.md", "docs/versions/8.0/a.md", "docs/deep/b.md", "cc/r.bzl",
                "cc/n.md", "tests/t.bzl", "top.md", "assets/logo.png"]:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content of " + rel)
    return base


def _rels(src, root=None):
    base = root or src.path
    return sorted(str(f.relative_to(base)) for f in rtfm.iter_source_files(src, root))


def test_allowlist_selects_only_listed_types(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, ext_allowlist=frozenset({".bzl"}))
    assert _rels(s) == ["cc/r.bzl", "tests/t.bzl"]


def test_blocklist_selects_everything_else(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, ext_blocklist=frozenset({".bzl"}))
    assert _rels(s) == ["cc/n.md", "docs/a.md", "docs/deep/b.md",
                        "docs/versions/8.0/a.md", "top.md"]   # .png via the default blocklist


def test_allowlist_never_consults_the_default_blocklist(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, ext_allowlist=frozenset({".png"}))
    assert _rels(s) == ["assets/logo.png"]     # an explicit allowlist wins over the default


def test_exclude_prefix_carves_out_of_an_include(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, paths=("docs",),
                    exclude_paths=("docs/versions",), ext_allowlist=frozenset({".md"}))
    assert _rels(s) == ["docs/a.md", "docs/deep/b.md"]


def test_exclude_prefix_with_no_include_applies_to_the_whole_tree(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, exclude_paths=("docs", "tests"),
                    ext_allowlist=frozenset({".md", ".bzl"}))
    assert _rels(s) == ["cc/n.md", "cc/r.bzl", "top.md"]


def test_exclude_does_not_match_a_partial_segment(tmp_path):
    t = _tree(tmp_path / "c")
    (t / "docsmore").mkdir()
    (t / "docsmore" / "x.md").write_text("x")
    s = rtfm.Source(name="s", type="dir", path=t, exclude_paths=("docs",),
                    ext_allowlist=frozenset({".md"}))
    assert "docsmore/x.md" in _rels(s)


def test_a_path_may_name_a_single_file(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, paths=("top.md",),
                    ext_allowlist=frozenset({".md"}))
    assert _rels(s) == ["top.md"]


def test_overlapping_include_prefixes_do_not_duplicate(tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="s", type="dir", path=t, paths=("docs", "docs/deep"),
                    ext_allowlist=frozenset({".md"}))
    assert _rels(s) == ["docs/a.md", "docs/deep/b.md", "docs/versions/8.0/a.md"]


def test_hidden_dirs_are_skipped(tmp_path):
    t = _tree(tmp_path / "c")
    (t / ".git").mkdir()
    (t / ".git" / "h.md").write_text("h")
    s = rtfm.Source(name="s", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    assert not any(r.startswith(".git") for r in _rels(s))


def test_root_override_is_used_for_managed_clones(tmp_path):
    t = _tree(tmp_path / "clone")
    s = rtfm.Source(name="s", type="git_repo", url="x", paths=("cc",),
                    ext_allowlist=frozenset({".bzl"}))
    assert s.path is None
    assert _rels(s, t) == ["cc/r.bzl"]


def test_reindex_dir_source_respects_scope(home, tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="scoped", type="dir", path=t, paths=("cc",),
                    ext_allowlist=frozenset({".bzl", ".md"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)
    assert summary["files_seen"] == 2
    rels = {r[0] for r in conn.execute(
        "SELECT relpath FROM locations WHERE source='scoped'")}
    assert rels == {"cc/n.md", "cc/r.bzl"}


def test_reindex_git_repo_source_respects_scope(home, tmp_path, git_branch):
    remote, seed, branch = make_git_repo(tmp_path, git_branch)
    for rel in ["docs/a.md", "cc/r.bzl", "tests/t.bzl"]:
        p = seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content of " + rel)
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "add"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", branch], capture_output=True)
    s = rtfm.Source(name="g", type="git_repo", url=str(remote), ref=branch,
                    paths=("cc",), ext_allowlist=frozenset({".bzl"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)
    assert summary.get("error") is None
    rels = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='g'")}
    assert rels == {"cc/r.bzl"}


def test_search_finds_content_in_a_declared_extension(home, tmp_path):
    t = tmp_path / "c"
    (t / "cc").mkdir(parents=True)
    (t / "cc" / "rule.bzl").write_text(
        "\n".join(["# rule impl"] * 3 + ['    shared_lib_name = attr.string(doc = "the name")']))
    s = rtfm.Source(name="bz", type="dir", path=t, ext_allowlist=frozenset({".bzl"}))
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, s)
    hits = rtfm.search_index(conn, "shared_lib_name", source="bz")
    assert hits and hits[0]["locations"][0]["relpath"] == "cc/rule.bzl"


def test_a_prefix_contributing_nothing_warns(home, tmp_path):
    t = _tree(tmp_path / "c")
    # 'assets' exists but holds only .png, which the allowlist does not select.
    s = rtfm.Source(name="q", type="dir", path=t, paths=("assets", "cc"),
                    ext_allowlist=frozenset({".bzl"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)
    assert summary["files_seen"] == 1
    assert any("PATH CONTRIBUTES NOTHING" in w and "'assets'" in w
               for w in summary["path_warnings"])
    assert not any("'cc'" in w for w in summary["path_warnings"])


def test_a_prefix_that_does_not_exist_warns(home, tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="typo", type="dir", path=t, paths=("doc",),
                    ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    assert any("PATH CONTRIBUTES NOTHING" in w and "'doc'" in w
               for w in rtfm.reindex_source(conn, s)["path_warnings"])


def test_no_path_warnings_when_every_prefix_contributes(home, tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="ok", type="dir", path=t, paths=("cc", "docs"),
                    ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    assert rtfm.reindex_source(conn, s)["path_warnings"] == []


# --- handling routines: one dispatch, extensible (ADR 0014) ------------------

def test_routines_dispatch_pdf_markup_and_catch_all(tmp_path):
    assert rtfm._routine_for(".pdf").name == "pdf"
    assert rtfm._routine_for(".md").name == "markup"
    assert rtfm._routine_for(".mdx").name == "markup"
    assert rtfm._routine_for(".bzl").name == "text"       # catch-all
    assert rtfm._routine_for("").name == "text"           # extensionless


def test_the_fallback_is_not_a_member_of_the_ordered_registry():
    # Ordering cannot shadow a routine because the fallback is not in the sequence at all.
    assert rtfm.DEFAULT_ROUTINE not in rtfm.ROUTINES
    assert all(r.exts for r in rtfm.ROUTINES)        # every entry is a real match


def test_body_and_signal_dispatch_agree_on_the_same_routine(tmp_path):
    # One registry drives both, so a file can never be selected for body extraction by one
    # rule and for signal extraction by a different one.
    f = tmp_path / "x.bzl"
    f.write_text("# a comment heading\nshared_lib_name = 1\n")
    assert rtfm._rows_for_file(f)                     # body extracted
    assert rtfm._doc_signal_for_file(f) == ("", "")   # catch-all carries no heading signal


def test_markup_routine_still_extracts_headings(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# Title\n\nbody text\n\n## Section\n")
    title, headings = rtfm._doc_signal_for_file(f)
    assert title == "Title" and "Section" in headings


# --- declared scope is a staleness trigger (ADR 0014) ------------------------

def test_source_meta_has_config_scope_column(home):
    conn = rtfm.get_index_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(source_meta)")}
    assert "config_scope" in cols


def test_scope_is_order_independent_and_change_sensitive(tmp_path):
    a = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("cc", "docs"),
                    ext_allowlist=frozenset({".bzl", ".md"}))
    b = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("docs", "cc"),
                    ext_allowlist=frozenset({".md", ".bzl"}))
    c = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("cc",),
                    ext_allowlist=frozenset({".bzl", ".md"}))
    d = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("cc", "docs"),
                    exclude_paths=("cc/private",), ext_allowlist=frozenset({".bzl", ".md"}))
    e = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("cc", "docs"),
                    ext_blocklist=frozenset({".bzl", ".md"}))
    assert rtfm._source_scope(a) == rtfm._source_scope(b)
    assert rtfm._source_scope(a) != rtfm._source_scope(c)
    assert rtfm._source_scope(a) != rtfm._source_scope(d)      # exclude_paths counts
    assert rtfm._source_scope(a) != rtfm._source_scope(e)      # which list it is counts


def _seed_bzl_repo(tmp_path, branch):
    remote, seed, branch = make_git_repo(tmp_path, branch)
    (seed / "r.bzl").write_text('shared_lib_name = "x"\n')
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "bzl"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", branch], capture_output=True)
    return remote, seed, branch


def test_changing_the_extension_list_makes_a_git_repo_stale(home, tmp_path, git_branch):
    remote, _seed, branch = _seed_bzl_repo(tmp_path, git_branch)
    conn = rtfm.get_index_db()
    narrow = rtfm.Source(name="g", type="git_repo", url=str(remote), ref=branch,
                         ext_allowlist=frozenset({".md"}))
    rtfm.reindex_source(conn, narrow)
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, narrow)[1] is False
    wide = rtfm.Source(name="g", type="git_repo", url=str(remote), ref=branch,
                       ext_allowlist=frozenset({".md", ".bzl"}))
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, wide)[1] is True
    rtfm.reindex_source(conn, wide)
    rels = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='g'")}
    assert "r.bzl" in rels


def test_changing_paths_alone_makes_a_git_repo_stale(home, tmp_path, git_branch):
    remote, _seed, branch = _seed_bzl_repo(tmp_path, git_branch)
    conn = rtfm.get_index_db()
    a = rtfm.Source(name="p", type="git_repo", url=str(remote), ref=branch,
                    ext_allowlist=frozenset({".bzl", ".md"}))
    rtfm.reindex_source(conn, a)
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, a)[1] is False
    b = rtfm.Source(name="p", type="git_repo", url=str(remote), ref=branch,
                    paths=("nowhere",), ext_allowlist=frozenset({".bzl", ".md"}))
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, b)[1] is True      # the paths half must count too


def test_scope_change_beats_a_managed_sha_pin(home, tmp_path, git_branch):
    remote, seed, _branch = _seed_bzl_repo(tmp_path, git_branch)
    sha = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    conn = rtfm.get_index_db()
    pinned = rtfm.Source(name="m", type="git_repo", url=str(remote), ref=sha,
                         ext_allowlist=frozenset({".md"}))
    rtfm.reindex_source(conn, pinned)
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, pinned)[1] is False
    wider = rtfm.Source(name="m", type="git_repo", url=str(remote), ref=sha,
                        ext_allowlist=frozenset({".md", ".bzl"}))
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, wider)[1] is True


def test_scope_change_beats_a_linked_sha_pin(home, tmp_path, git_branch):
    """The managed case never reaches the pin short-circuit — managed mode compares against
    the pin itself for any ref. The short-circuit that the scope check must be
    ordered ahead of exists only in the linked branch, so pin it there."""
    remote, _seed, branch = _seed_bzl_repo(tmp_path, git_branch)
    dest = tmp_path / "linked"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    sha = rtfm._git_current_commit(dest)
    subprocess.run(["git", "-C", str(dest), "checkout", sha], capture_output=True)
    conn = rtfm.get_index_db()
    pinned = rtfm.Source(name="lp", type="git_repo", path=dest, url=str(remote), ref=sha,
                         ext_allowlist=frozenset({".md"}))
    rtfm.reindex_source(conn, pinned)
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, pinned)[1] is False
    wider = rtfm.Source(name="lp", type="git_repo", path=dest, url=str(remote), ref=sha,
                        ext_allowlist=frozenset({".md", ".bzl"}))
    rtfm._staleness_cache.clear()
    assert rtfm._stale_delta(conn, wider)[1] is True


def test_narrowing_purges_the_excluded_files(home, tmp_path):
    t = _tree(tmp_path / "c")
    conn = rtfm.get_index_db()
    wide = rtfm.Source(name="n", type="dir", path=t,
                       ext_allowlist=frozenset({".md", ".bzl"}))
    rtfm.reindex_source(conn, wide)
    assert any(r[0].endswith(".bzl") for r in conn.execute(
        "SELECT relpath FROM locations WHERE source='n'"))
    narrow = rtfm.Source(name="n", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    rtfm.reindex_source(conn, narrow)
    rels = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='n'")}
    assert not any(r.endswith(".bzl") for r in rels)


# --- stage 2: decoding is strict, and its zero is reported (ADR 0015) --------

def test_a_binary_file_is_an_extraction_error_not_garbage(home, tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "blob.dat").write_bytes(bytes(range(256)) * 20)
    (t / "ok.md").write_text("alpha keyword\n")
    s = rtfm.Source(name="b", type="dir", path=t,
                    ext_allowlist=frozenset({".dat", ".md"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)
    assert summary["errors"] == 1
    errs = [r[0] for r in conn.execute("SELECT error FROM contents WHERE extracted_ok=0")]
    assert any("UnicodeDecodeError" in e for e in errs)
    assert rtfm.search_index(conn, "keyword", source="b")   # one bad file blocks nothing


def test_no_replacement_characters_reach_the_index(home, tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "blob.dat").write_bytes(b"\xff\xfe\x00text-like\x00\xff")
    s = rtfm.Source(name="r", type="dir", path=t, ext_allowlist=frozenset({".dat"}))
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, s)
    texts = [r[0] for r in conn.execute("SELECT text FROM content_fts")]
    assert not any("�" in x for x in texts)


def test_valid_utf8_with_non_ascii_still_indexes(home, tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "u.md").write_text("naïve café — em dash ✓ keyword\n", encoding="utf-8")
    s = rtfm.Source(name="u", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    assert rtfm.reindex_source(conn, s)["errors"] == 0
    assert rtfm.search_index(conn, "keyword", source="u")


def test_a_source_where_nothing_decodes_says_so(home, tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.dat").write_bytes(bytes(range(256)))
    (t / "b.dat").write_bytes(bytes(range(256)))
    s = rtfm.Source(name="allbad", type="dir", path=t, ext_allowlist=frozenset({".dat"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)
    # Stage 1 let files through; stage 2 emptied the source, so stage 2 names itself.
    assert summary["files_seen"] == 2
    assert any("NOTHING COULD BE READ" in w for w in summary["path_warnings"])


def test_an_exclude_prefix_that_excludes_nothing_warns(home, tmp_path):
    """An include prefix that matches nothing warns; an exclude prefix that matches nothing
    did not. A misspelled exclusion is a no-op, so the user believes a subtree is out of the
    corpus when every file in it is still indexed — the wrong direction to be silent in."""
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="x", type="dir", path=t, exclude_paths=("docs/verisons",),
                    ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    warns = rtfm.reindex_source(conn, s)["path_warnings"]
    assert any("EXCLUDES NOTHING" in w and "'!docs/verisons'" in w for w in warns)


def test_an_exclude_prefix_that_works_does_not_warn(home, tmp_path):
    t = _tree(tmp_path / "c")
    s = rtfm.Source(name="y", type="dir", path=t, exclude_paths=("docs/versions",),
                    ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    assert rtfm.reindex_source(conn, s)["path_warnings"] == []


# --- stage 2 ACCESS: can rtfm read the bytes at all? (ADR 0015) --------------


def test_an_unreadable_file_does_not_crash_the_reindex(home, tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "ok.md").write_text("readable keyword\n")
    unopenable(t / "locked.md")
    s = rtfm.Source(name="a", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)          # used to raise PermissionError
    assert summary["access_errors"] == 1
    assert rtfm.search_index(conn, "keyword", source="a")   # the readable file still indexes


def test_an_unreadable_file_is_named_in_the_report(home, tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "ok.md").write_text("fine\n")
    unopenable(t / "locked.md")
    s = rtfm.Source(name="a", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    warns = rtfm._summary_warnings(s, rtfm.reindex_source(conn, s))
    assert any("COULD NOT OPEN" in w and "locked.md" in w for w in warns)


def test_a_directory_the_walk_cannot_enter_is_reported(home, tmp_path, unopenable):
    t = tmp_path / "c"
    (t / "vault").mkdir(parents=True)
    (t / "vault" / "secret.md").write_text("hidden\n")
    (t / "ok.md").write_text("fine\n")
    unopenable(t / "vault", directory=True)
    s = rtfm.Source(name="a", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    warns = rtfm._summary_warnings(s, rtfm.reindex_source(conn, s))
    # rglob swallowed this at scandir, so the subtree was invisible to every code path.
    # It is SELECT's report, not ACCESS's: nothing under it was ever considered.
    reports = rtfm.reindex_source(conn, s)["path_warnings"]
    assert any("SOURCE INCOMPLETE" in w and "vault" in w for w in reports)
    assert not any("COULD NOT OPEN" in w for w in warns)


def test_access_and_decode_failures_are_counted_separately(home, tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "ok.md").write_text("fine\n")
    (t / "binary.md").write_bytes(bytes(range(256)))
    unopenable(t / "locked.md")
    s = rtfm.Source(name="a", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    summary = rtfm.reindex_source(conn, s)
    # Different questions, different remedies: chmod versus convert-or-exclude.
    assert summary["access_errors"] == 1
    assert summary["errors"] == 1
    warns = rtfm._summary_warnings(s, summary)
    assert any("COULD NOT OPEN" in w for w in warns)
    assert any("COULD NOT READ" in w for w in warns)
