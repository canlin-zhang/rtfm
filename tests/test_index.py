# tests/test_index.py
import os
import sqlite3
import subprocess

from conftest import make_git_repo

import rtfm_server as rtfm


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
    locators. Unlocks Sphinx doc trees. Fails before they're in TEXT_EXTS (files skipped)."""
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


# --- _stale_delta for git_repo ---

def test_stale_delta_git_repo_current_is_not_stale(home, tmp_path, git_branch):
    """A git_repo whose indexed commit matches origin/<ref> is not stale."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    commit = rtfm._git_current_commit(dest)
    commit_date = rtfm._git_commit_date(dest, "HEAD")
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date) VALUES(?,?,?)",
        ("specs", commit, commit_date))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                       url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta(conn, src)
    assert stale is False
    assert changed == 0


def test_stale_delta_git_repo_behind_is_stale(home, tmp_path, git_branch):
    """A managed git_repo whose indexed commit is behind origin/<ref> is stale —
    rtfm owns the clone, so the fetch-and-compare applies. (Linked clones are
    read-only: only the tree's HEAD matters there — see
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
        "INSERT INTO source_meta(source, git_commit, git_commit_date) VALUES(?,?,?)",
        ("specs", old_commit, "2025-01-01T00:00:00+00:00"))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta(conn, src)
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
        "INSERT INTO source_meta(source, git_commit, git_commit_date) VALUES(?,?,?)",
        ("specs", old_commit, "2025-01-01T00:00:00+00:00"))
    conn.commit()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=branch)
    changed, stale = rtfm._stale_delta(conn, src)
    assert stale is False  # remote moved, tree didn't — nothing to reindex

    # The USER refreshes their own clone (their fetch + checkout) — stale now
    subprocess.run(["git", "-C", str(dest), "fetch", "origin"], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"],
                    capture_output=True)
    changed, stale = rtfm._stale_delta(conn, src)
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
    changed, stale = rtfm._stale_delta(conn, src)
    assert stale is True


def test_default_branch_parses_remote_head(home, tmp_path, git_branch):
    """_default_branch reads the remote's HEAD branch, not a hardcoded guess."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    assert rtfm._default_branch(dest) == branch
