# tests/test_index.py
import hashlib
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


def test_a_handling_failure_is_reported_on_every_run(home, tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=tmp_path, ext_blocklist=frozenset())

    first = _index(conn, src, tmp_path)
    assert [p.position for p in first.handled.problems] == ["broken.pdf"]

    second = _index(conn, src, tmp_path)
    assert [p.position for p in second.handled.problems] == [], (
        "step 3B reports what THIS run failed to handle; nothing was re-extracted")
    assert any("broken.pdf" in m for m in rtfm.report_all(conn, src, second)), (
        "a file that is still broken must still be reported")
    assert second.cache.newly_extracted == 0, "and must not be re-extracted to say so"


def test_a_repaired_file_stops_being_reported(home, tmp_path):
    (tmp_path / "f.md").write_bytes(b"\xff\xfe broken\n")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=tmp_path, ext_blocklist=frozenset())
    assert _index(conn, src, tmp_path).handled.problems
    (tmp_path / "f.md").write_text("now findable keyword")
    assert _index(conn, src, tmp_path).handled.problems == []


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
    locators. Unlocks Sphinx doc trees. Fails before they're selected by the manifest's
    extension list (files skipped)."""
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
    with line locators; component tags are inert text. Fails before `.mdx` is selected by
    the manifest's extension list (the file is skipped, and the whole site indexes as
    nothing)."""
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


# --- _freshness for git_repo ---

def test_freshness_git_repo_current_is_not_stale(home, tmp_path, git_branch):
    """Exercises the linked not-stale path at a fresh clone (HEAD == origin/<ref>,
    clean tree); the HEAD-vs-origin distinction is pinned by
    test_freshness_git_repo_linked_head_moved_is_stale."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    commit = rtfm._git_current_commit(dest)
    commit_date = rtfm._git_commit_date(dest, "HEAD")
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                       url=str(remote), ref=branch)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", commit, commit_date, rtfm._config_scope(src)))
    conn.commit()
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is False
    assert changed == 0


def test_freshness_linked_dirty_is_stale(home, tmp_path, git_branch):
    """A linked git_repo with uncommitted edits at the indexed commit is stale —
    the reindex refusal then warns loudly on search instead of serving silently
    absent edits (ADR 0013: dirty = refuse)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    commit = rtfm._git_current_commit(dest)
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=branch)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", commit, "2025-01-01T00:00:00+00:00", rtfm._config_scope(src)))
    conn.commit()
    (dest / "a.md").write_text("uncommitted v2\n")
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is True


def test_freshness_pinned_sha_never_stale(home, tmp_path, git_branch):
    """A pinned-SHA source is never stale once indexed — the pin never moves,
    staleness is undefined (ADR 0013)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    sha = rtfm._git_current_commit(dest)
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=sha)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", sha, "2025-01-01T00:00:00+00:00", rtfm._config_scope(src)))
    conn.commit()
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is False


def test_freshness_git_repo_behind_is_stale(home, tmp_path, git_branch):
    """A managed git_repo whose indexed commit is behind origin/<ref> is stale —
    rtfm owns the clone, so the fetch-and-compare applies. (Linked clones are
    read-only: only the tree's HEAD or a dirty tree matters there — see
    test_freshness_git_repo_linked_head_moved_is_stale.)"""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="v1\n")
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    old_commit = rtfm._git_current_commit(dest)
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", branch], capture_output=True)
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref=branch)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", old_commit, "2025-01-01T00:00:00+00:00", rtfm._config_scope(src)))
    conn.commit()
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is True


def test_freshness_git_repo_linked_head_moved_is_stale(home, tmp_path, git_branch):
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
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=branch)
    conn = rtfm.get_index_db()
    conn.execute(
        "INSERT INTO source_meta(source, git_commit, git_commit_date, config_scope) "
        "VALUES(?,?,?,?)",
        ("specs", old_commit, "2025-01-01T00:00:00+00:00", rtfm._config_scope(src)))
    conn.commit()
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is False  # remote moved, tree didn't — nothing to reindex

    # The USER refreshes their own clone (their fetch + checkout) — stale now
    subprocess.run(["git", "-C", str(dest), "fetch", "origin"], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"],
                    capture_output=True)
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is True


def test_freshness_git_repo_no_source_meta_is_stale(home, tmp_path, git_branch):
    """A git_repo with no source_meta row is always stale (never been indexed)."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                       url=str(remote), ref=branch)
    changed, stale = rtfm._repo_freshness(conn, src)
    assert stale is True


def test_default_branch_parses_remote_head(home, tmp_path, git_branch):
    """_default_branch reads the remote's HEAD branch, not a hardcoded guess."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    assert rtfm._default_branch(dest) == branch


def test_step_result_classifies_its_own_outcome():
    clean = rtfm.StepResult(kept=[1, 2], problems=[])
    partial = rtfm.StepResult(
        kept=[1], problems=[rtfm.Problem("a.md", "nope")]
    )
    empty = rtfm.StepResult(
        kept=[], problems=[rtfm.Problem("a.md", "nope")]
    )
    assert (
        rtfm._is_clean(clean),
        rtfm._is_partial(clean),
        rtfm._is_empty(clean),
    ) == (True, False, False)
    assert (
        rtfm._is_clean(partial),
        rtfm._is_partial(partial),
        rtfm._is_empty(partial),
    ) == (False, True, False)
    assert (
        rtfm._is_clean(empty),
        rtfm._is_partial(empty),
        rtfm._is_empty(empty),
    ) == (False, False, True)


def test_a_problem_names_a_position_not_a_hash():
    p = rtfm.Problem("docs/a.md", "Permission denied")
    assert p.position == "docs/a.md"
    assert p._fields == ("position", "reason")


def test_one_registry_drives_body_and_signal(tmp_path):
    # These dispatched independently and could disagree about a file — a .bzl was
    # selected by one and ignored by the other, so it indexed to zero rows.
    assert rtfm._routine_for(".pdf").name == "pdf"
    assert rtfm._routine_for(".md").name == "markup"
    assert rtfm._routine_for(".bzl").name == "text"
    assert rtfm._routine_for("").name == "text"


def test_the_fallback_is_not_a_member_of_the_ordered_registry():
    # Inside the tuple it would match unconditionally, shadowing anything after it.
    assert rtfm.DEFAULT_ROUTINE not in rtfm.ROUTINES
    assert all(r.exts for r in rtfm.ROUTINES)


def test_markup_routine_still_extracts_headings(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# Title\n\nbody text\n\n## Section\n")
    title, headings = rtfm._doc_signal_for_file(f)
    assert title == "Title" and "Section" in headings


def _reached(*paths):
    """Step 1's product for a test that starts at step 2 or 3A: access() records the mtime
    it stats while scanning, so nothing downstream stats again (ADR 0015).

    Falls back to 0.0 for a path that isn't on disk — step 2 is policy over names and never
    looks at content or mtime, so its tests name files they never create."""
    return [rtfm._Reached(p, p.stat().st_mtime if p.exists() else 0.0) for p in paths]


def _index(conn, src, root):
    """Scan then ingest — what the production callers compose. A wrapper in the server would
    exist only for these call sites, so it lives here instead."""
    return rtfm._ingest(conn, src, root, rtfm._scan(src, root))


def _selected(src, root=None):
    """The names step 2 kept, for a test that cares about selection policy only."""
    return [r.path.name for r in rtfm._scan(src, root or src.path).wanted]


def _src(tmp_path, **kw):
    kw.setdefault("ext_blocklist", frozenset())
    return rtfm.Source(name="s", type="dir", path=tmp_path, **kw)


def test_select_returns_a_bare_list_not_a_step_result(tmp_path):
    # Step 2 asks about intent, where rtfm has no standing to call 90% filtered a partial
    # success. A bare list means there is nowhere to record a step-2 partial outcome.
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    got = rtfm.select(_reached(t / "a.md"), t, _src(t))
    assert isinstance(got, list)
    assert not isinstance(got, rtfm.StepResult)


def test_select_keeps_supported_and_drops_the_rest(tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    paths = _reached(t / "a.md", t / "b.png", t / "c.pdf", t / "d.rst")
    got = rtfm.select(paths, t, _src(t))
    assert sorted(r.path.name for r in got) == ["a.md", "c.pdf", "d.rst"]


def test_allowlist_selects_only_those_types(home, tmp_path):
    for n in ("a.md", "b.bzl", "c.png", "d.txt"):
        (tmp_path / n).write_text("x")
    src = rtfm.Source(name="s", type="dir", path=tmp_path,
                      ext_allowlist=frozenset({".md", ".bzl"}))
    got = set(_selected(src))
    assert got == {"a.md", "b.bzl"}


def test_blocklist_selects_everything_but_the_union_with_rtfms_defaults(home, tmp_path):
    for n in ("a.md", "b.py", "c.png", "d.log"):
        (tmp_path / n).write_text("x")
    src = _src(tmp_path, ext_blocklist=frozenset({".log"}))
    got = set(_selected(src))
    assert got == {"a.md", "b.py"}          # .png from the default set, .log from the user's


def test_an_allowlist_never_consults_the_default_blocklist(home, tmp_path):
    (tmp_path / "a.png").write_text("x")
    src = rtfm.Source(name="s", type="dir", path=tmp_path,
                      ext_allowlist=frozenset({".png"}))
    assert _selected(src) == ["a.png"]


def test_paths_scopes_to_a_prefix(home, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "src" / "b.md").write_text("x")
    src = _src(tmp_path, paths=("docs",))
    assert _selected(src) == ["a.md"]


def test_an_exclusion_beats_an_inclusion(home, tmp_path):
    (tmp_path / "docs" / "versions").mkdir(parents=True)
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "docs" / "versions" / "old.md").write_text("x")
    src = _src(tmp_path, paths=("docs",), exclude_paths=("docs/versions",))
    assert _selected(src) == ["a.md"]


def test_an_exclusion_alone_scopes_the_whole_tree_minus_that_prefix(home, tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "drop").mkdir()
    (tmp_path / "keep" / "a.md").write_text("x")
    (tmp_path / "drop" / "b.md").write_text("x")
    src = _src(tmp_path, exclude_paths=("drop",))
    assert _selected(src) == ["a.md"]


def test_a_prefix_does_not_match_a_partial_directory_name(home, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docsets").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "docsets" / "b.md").write_text("x")
    src = _src(tmp_path, paths=("docs",))
    assert _selected(src) == ["a.md"]


def test_an_exclusion_that_is_a_prefix_of_an_inclusion_selects_nothing(home, tmp_path):
    # paths=("docs/api",) narrows to a subtree that exclude_paths=("docs",) then excludes
    # wholesale — exclusion wins unconditionally (ADR 0014), so this is the "looks correct,
    # indexes nothing" shape the ADR warns about, not a bug in either clause alone.
    (tmp_path / "docs" / "api").mkdir(parents=True)
    (tmp_path / "docs" / "api" / "a.md").write_text("x")
    src = _src(tmp_path, paths=("docs/api",), exclude_paths=("docs",))
    assert _selected(src) == []


def test_a_root_level_file_is_not_selected_under_a_restrictive_paths(home, tmp_path):
    # readme.md sits at the source root, outside the "docs" prefix — paths narrows to a
    # subtree, it never implicitly keeps the root alongside it.
    (tmp_path / "readme.md").write_text("x")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    src = _src(tmp_path, paths=("docs",))
    assert _selected(src) == ["a.md"]


def test_markup_exts_is_not_a_selection_set(home, tmp_path):
    # The whole point of ADR 0014: a .bzl file is selected by the manifest and handled by
    # the plain-text routine. TEXT_EXTS deciding both is the defect being deleted.
    assert not hasattr(rtfm, "TEXT_EXTS")
    (tmp_path / "r.bzl").write_text("def cc_shared_library(shared_lib_name): pass")
    src = rtfm.Source(name="s", type="dir", path=tmp_path,
                      ext_allowlist=frozenset({".bzl"}))
    conn = rtfm.get_index_db()
    _index(conn, src, tmp_path)
    assert rtfm.search_index(conn, "shared_lib_name", source="s")


def test_a_dotdot_paths_entry_selects_nothing(home, tmp_path):
    # Task 1 rejects glob characters in `paths` but not a `..` segment. What's actually
    # guaranteed: selection can never leave the source root, because the prefix match is
    # against a relpath _rel produces relative to `base`, and access() only ever enumerates
    # beneath that root — a relpath can neither render as absolute nor climb above it, so a
    # ".." entry can't match anything.
    (tmp_path / "a.md").write_text("x")
    assert _selected(_src(tmp_path, paths=("..",))) == []


def test_a_leading_slash_in_paths_normalizes_to_a_root_anchored_prefix(home, tmp_path):
    # A leading "/" in a manifest's `paths` entry is root-anchoring, the same convention
    # .gitignore uses — _normalize_paths strips it, so "/docs" becomes the source-relative
    # prefix "docs" and DOES select the docs/ subtree. Going through _source_from_table (the
    # real manifest parsing path), not _src: _src builds a Source directly and bypasses
    # _normalize_paths, which is why an earlier version of this test could (wrongly) claim
    # a leading-"/" entry matches nothing.
    (tmp_path / "docs").mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "etc" / "passwd.md").write_text("x")
    src = rtfm._source_from_table({
        "name": "s", "type": "dir", "path": str(tmp_path),
        "paths": ["/docs"], "ext_blocklist": [],
    })
    assert src.paths == ("docs",)
    assert _selected(src) == ["a.md"]


def test_access_returns_reachable_positions(tmp_path):
    t = tmp_path / "c"
    (t / "sub").mkdir(parents=True)
    (t / "a.md").write_text("a")
    (t / "sub" / "b.md").write_text("b")
    s = rtfm.Source(name="s", type="dir", path=t)
    got = rtfm.access(s)
    assert sorted(r.path.name for r in got.kept) == ["a.md", "b.md"]
    assert all(r.mtime == r.path.stat().st_mtime for r in got.kept)   # step 1 carries freshness
    assert got.problems == []
    assert rtfm._is_clean(got)


def test_access_names_a_directory_it_cannot_enter(tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    unopenable(t / "vault", directory=True)
    got = rtfm.access(rtfm.Source(name="s", type="dir", path=t))
    # rglob swallows this inside scandir and yields nothing, which is indistinguishable from
    # an empty directory — so the subtree was invisible to every code path.
    assert rtfm._is_partial(got)
    assert any("vault" in p.position for p in got.problems)


def test_access_names_a_file_it_cannot_open(tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    unopenable(t / "locked.md")
    got = rtfm.access(rtfm.Source(name="s", type="dir", path=t))
    assert any(p.position == "locked.md" for p in got.problems)
    assert [r.path.name for r in got.kept] == ["a.md"]


def test_access_reports_unfiltered(tmp_path, unopenable):
    # We cannot filter what we could not read — an inaccessible directory might hold exactly
    # the files the user wants, and its contents are unknowable from outside (ADR 0015).
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    unopenable(t / "image.png")
    got = rtfm.access(rtfm.Source(name="s", type="dir", path=t))
    assert any(p.position == "image.png" for p in got.problems)


def test_read_hashes_what_it_can(tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("alpha")
    got = rtfm.read_bytes_for(_reached(t / "a.md"), t, {})
    assert len(got.kept) == 1
    path, sha, mtime = got.kept[0]
    assert path.name == "a.md" and len(sha) == 64
    assert got.problems == []


def test_read_names_a_file_deleted_since_the_scan(tmp_path):
    # Step 1 stats, step 3A reads, and a file can vanish in between — the race ADR 0015 gives
    # step 3A. A cache hit must not skip the check: reusing the stored sha for a file that is
    # gone keeps its row and reports nothing, and the index then serves a path that 404s.
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("alpha")
    scanned = _reached(f)                       # step 1 saw it
    sha = rtfm.read_bytes_for(scanned, t, {}).kept[0].sha
    f.unlink()                                  # ...and it goes before step 3A gets there
    got = rtfm.read_bytes_for(scanned, t, {"a.md": (sha, scanned[0].mtime)})
    assert got.kept == []
    assert [p.position for p in got.problems] == ["a.md"]


def test_read_keeps_the_scans_mtime_against_new_content(tmp_path):
    # The mtime stored is step 1's, not a fresh one. A file edited between the scan and the
    # read is hashed as it is now, but recorded with the older mtime, so the next run sees a
    # mismatch and re-reads once. The reverse — a newer mtime against older bytes — would
    # make the stale content read as fresh forever.
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("alpha")
    scanned = _reached(f)
    f.write_text("beta")                        # changed after the scan...
    later = scanned[0].mtime + 10
    os.utime(f, (later, later))                 # ...landing with a later mtime, deterministically
    got = rtfm.read_bytes_for(scanned, t, {}).kept[0]
    assert got.sha == hashlib.sha256(b"beta").hexdigest()      # hashed as it is now
    assert got.mtime == scanned[0].mtime                       # recorded as it was scanned
    assert got.mtime < f.stat().st_mtime                       # i.e. older, never newer


def test_read_names_a_file_whose_bytes_it_cannot_get(tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("alpha")
    unopenable(t / "locked.md")
    got = rtfm.read_bytes_for(_reached(t / "a.md", t / "locked.md"), t, {})
    # One unreadable file used to raise straight out of the reindex.
    assert [p.position for p in got.problems] == ["locked.md"]
    assert len(got.kept) == 1


def test_read_reuses_a_cached_hash(tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("alpha")
    mtime = f.stat().st_mtime
    got = rtfm.read_bytes_for(_reached(f), t, {"a.md": ("cafebabe", mtime)})
    assert got.kept[0][1] == "cafebabe"      # no re-hash when the key matches


def test_handle_fans_a_failed_content_out_to_every_position(home, tmp_path):
    # Three byte-identical corrupt files are one extraction and three broken files. Reporting
    # the sha would make every consumer convert contents into files — the conversion that
    # printed a content count as "N file(s)".
    t = tmp_path / "c"
    t.mkdir()
    bad = b"%PDF-1.4 not really a pdf" + bytes(range(256))
    for n in ("v1.pdf", "v2.pdf", "v3.pdf"):
        (t / n).write_bytes(bad)
    read = rtfm.read_bytes_for(_reached(t / "v1.pdf", t / "v2.pdf", t / "v3.pdf"), t, {})
    sha = read.kept[0][1]
    got = rtfm.handle(read.kept, {sha}, t)
    assert sorted(p.position for p in got.problems) == ["v1.pdf", "v2.pdf", "v3.pdf"]
    assert all("pdf" not in p.reason.lower() or "utf-8" not in p.reason.lower()
               for p in got.problems)          # reported in the handler's own terms


def test_ingest_returns_each_step_s_result(home, tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("alpha keyword")
    conn = rtfm.get_index_db()
    got = _index(conn, rtfm.Source(name="s", type="dir", path=t), t)
    assert isinstance(got, rtfm.Indexed)
    assert [r.path.name for r in got.reachable.kept] == ["a.md"]
    assert [r.path.name for r in got.wanted] == ["a.md"]
    assert len(got.read.kept) == 1
    assert got.cache.unique_contents == 1


def test_an_unreadable_file_is_not_treated_as_vanished(home, tmp_path, unopenable):
    # Failing a step is not the same as ceasing to exist. Reconciling against what SUCCEEDED
    # would delete the row and GC the content — losing indexed content because a file's
    # permissions changed.
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("findable keyword")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=t)
    _index(conn, src, t)
    assert rtfm.search_index(conn, "keyword", source="s")
    f.chmod(0o000)
    try:
        got = _index(conn, src, t)
        assert got.cache.purged == 0
        rows = {r[0] for r in conn.execute(
            "SELECT relpath FROM locations WHERE source='s'")}
        assert rows == {"a.md"}
    finally:
        f.chmod(0o644)


def test_a_file_under_an_unreadable_directory_is_not_treated_as_vanished(
        home, tmp_path, unopenable):
    # Asking the filesystem about a path under a directory it cannot enter gets no usable
    # answer — Path.is_file() raises EACCES before 3.14 and reports a bare False from 3.14
    # on, which is indistinguishable from "gone". Only step 1's own report of the
    # unreachable directory settles it. The file is untouched; rtfm just can't see it now.
    t = tmp_path / "c"
    t.mkdir()
    (t / "vault").mkdir()
    (t / "vault" / "deep.md").write_text("findable keyword")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=t)
    _index(conn, src, t)
    assert rtfm.search_index(conn, "keyword", source="s")
    unopenable(t / "vault", directory=True)
    got = _index(conn, src, t)
    assert got.cache.purged == 0
    rows = {r[0] for r in conn.execute(
        "SELECT relpath FROM locations WHERE source='s'")}
    assert rows == {"vault/deep.md"}
    # Still searchable: the row and its content were never GC'd, so search_index answers
    # from what was already indexed — it does not need to re-read the now-unreachable file.
    assert rtfm.search_index(conn, "keyword", source="s")


def test_freshness_converges_after_an_unreadable_file(home, tmp_path):
    # Regression: _freshness used to build on_disk from iter_source_files, which routes
    # through access() and drops every position access() denies. The indexed row survives
    # the reconcile (see test above) — deliberately — so set(indexed) != set(on_disk) was
    # permanently true and the source read as stale on every single query, forever.
    # chmod directly (not the `unopenable` fixture): its write_text step would rewrite this
    # file's content — and so its mtime — which manufactures a real content change instead of
    # the plain permission-only case this regression is about.
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("findable keyword")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=t)
    _index(conn, src, t)
    f.chmod(0o000)
    try:
        _index(conn, src, t)  # reconcile round: the row survives, still unreadable
        assert rtfm._freshness(conn, src)[:3] == (0, False, False)
    finally:
        f.chmod(0o644)


def test_freshness_converges_after_an_unreadable_directory(home, tmp_path, unopenable):
    # Same convergence bug, but for a directory step 1 could not enter at all — the review
    # reported this case looping identically to the single-file one above.
    t = tmp_path / "c"
    t.mkdir()
    (t / "vault").mkdir()
    (t / "vault" / "deep.md").write_text("findable keyword")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=t)
    _index(conn, src, t)
    unopenable(t / "vault", directory=True)
    _index(conn, src, t)  # reconcile round: the row survives, still unreadable
    assert rtfm._freshness(conn, src)[:3] == (0, False, False)


def test_a_denied_source_root_does_not_purge_the_whole_source(home, tmp_path, unopenable):
    # The root is the one position _rel renders as "." — no relpath equals it or starts with
    # "./", so a prefix test alone never shadows anything and every row of the source reads as
    # vanished at once, content GC'd, while every file is still sitting on disk untouched.
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("findable keyword")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=t)
    _index(conn, src, t)
    unopenable(t, directory=True)
    got = _index(conn, src, t)
    assert got.reachable.problems == [rtfm.Problem(position=".", reason="Permission denied")]
    assert got.cache.purged == 0
    rows = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='s'")}
    assert rows == {"a.md"}
    assert rtfm.search_index(conn, "keyword", source="s")


def test_freshness_converges_after_a_denied_source_root(home, tmp_path, unopenable):
    # Same root blind spot on the freshness side: nothing shadowed means the surviving rows
    # never rejoin on_disk, so the source reads stale on every query and never converges.
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("findable keyword")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=t)
    _index(conn, src, t)
    unopenable(t, directory=True)
    _index(conn, src, t)  # reconcile round: the row survives, still unreachable
    assert rtfm._freshness(conn, src)[:3] == (0, False, False)


def test_a_de_selected_file_is_purged(home, tmp_path):
    # The manifest stops wanting .log; the file is untouched on disk. Step 2's answer is
    # the only one that decides what is in this source, so the row must go. Distinct bodies:
    # this test only asserts on `locations`, not on shared content (see the byte-identical
    # case below for that).
    (tmp_path / "keep.md").write_text("findable keyword")
    (tmp_path / "drop.log").write_text("a different findable keyword")
    conn = rtfm.get_index_db()
    wide = rtfm.Source(name="s", type="dir", path=tmp_path, ext_blocklist=frozenset())
    _index(conn, wide, tmp_path)
    rows = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='s'")}
    assert rows == {"keep.md", "drop.log"}

    narrow = rtfm.Source(name="s", type="dir", path=tmp_path,
                         ext_blocklist=frozenset({".log"}))
    got = _index(conn, narrow, tmp_path)
    assert got.cache.purged == 1
    rows = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='s'")}
    assert rows == {"keep.md"}


def test_a_de_selected_file_leaves_no_orphan_content(home, tmp_path):
    (tmp_path / "drop.log").write_text("unique content here")
    conn = rtfm.get_index_db()
    _index(conn, rtfm.Source(name="s", type="dir", path=tmp_path,
                                        ext_blocklist=frozenset()), tmp_path)
    assert conn.execute("SELECT count(*) FROM contents").fetchone()[0] == 1
    _index(conn, rtfm.Source(name="s", type="dir", path=tmp_path,
                                        ext_blocklist=frozenset({".log"})), tmp_path)
    assert conn.execute("SELECT count(*) FROM contents").fetchone()[0] == 0


def test_a_de_selected_files_content_survives_via_a_still_selected_twin(home, tmp_path):
    # keep.md and drop.log are byte-identical, so they share one contents row (one sha256).
    # De-selecting .log purges drop.log's *locations* row, but GC is global — "NOT IN (SELECT
    # sha256 FROM locations)" — not per-source, so the shared content is NOT collected while
    # keep.md's own locations row still references it, and keep.md must stay searchable
    # throughout. This is the mechanism that produced a false positive during review.
    body = "shared identical keyword body"
    (tmp_path / "keep.md").write_text(body)
    (tmp_path / "drop.log").write_text(body)
    conn = rtfm.get_index_db()
    wide = rtfm.Source(name="s", type="dir", path=tmp_path, ext_blocklist=frozenset())
    _index(conn, wide, tmp_path)
    assert conn.execute("SELECT count(*) FROM contents").fetchone()[0] == 1  # one shared sha
    assert rtfm.search_index(conn, "shared identical keyword", source="s")

    narrow = rtfm.Source(name="s", type="dir", path=tmp_path,
                         ext_blocklist=frozenset({".log"}))
    got = _index(conn, narrow, tmp_path)
    assert got.cache.purged == 1
    rows = {r[0] for r in conn.execute("SELECT relpath FROM locations WHERE source='s'")}
    assert rows == {"keep.md"}
    assert conn.execute("SELECT count(*) FROM contents").fetchone()[0] == 1  # content survives
    assert rtfm.search_index(conn, "shared identical keyword", source="s")


def test_key_order_does_not_change_the_scope(home, tmp_path):
    a = rtfm.Source(name="s", type="dir", path=tmp_path,
                    paths=("a", "b"), ext_allowlist=frozenset({".md", ".pdf"}))
    b = rtfm.Source(name="s", type="dir", path=tmp_path,
                    paths=("b", "a"), ext_allowlist=frozenset({".pdf", ".md"}))
    assert rtfm._config_scope(a) == rtfm._config_scope(b)


def test_config_scope_does_not_collide_on_a_comma_in_a_path(home, tmp_path):
    # A directory literally named "a,b" and the pair ("a", "b") joined on a comma used to
    # serialize to the same string — a git_repo source edited between the two configs read
    # as not-stale, defeating the exact check ADR 0014 added config_scope for. JSON encoding
    # keeps the two shapes distinct because "," inside a JSON string element is not the same
    # as "," used as JSON's own array separator.
    one_entry = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("a,b",))
    two_entries = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("a", "b"))
    assert rtfm._config_scope(one_entry) != rtfm._config_scope(two_entries)


def test_config_scope_distinguishes_declared_empty_from_undeclared(home, tmp_path):
    # ext_allowlist=frozenset() ("index nothing extra") and ext_allowlist=None ("not declared,
    # ext_blocklist governs instead") are different configurations and must scope differently.
    declared_empty = rtfm.Source(name="s", type="dir", path=tmp_path,
                                 ext_allowlist=frozenset())
    undeclared = rtfm.Source(name="s", type="dir", path=tmp_path, ext_allowlist=None)
    assert rtfm._config_scope(declared_empty) != rtfm._config_scope(undeclared)


def test_a_source_selecting_nothing_is_fresh_and_still_reported(home, tmp_path):
    # indexed == on_disk == {} reads as fresh, which is correct: nothing to index, nothing to
    # purge. It used to be forced stale so the pipeline would run and NOTHING SELECTED could
    # reach the user; the report now comes off the freshness handler's own scan instead, so
    # the source stays fresh AND stays reported (ADR 0014, ADR 0015).
    (tmp_path / "a.png").write_text("x")
    src = rtfm.Source(name="s", type="dir", path=tmp_path,
                      ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    _index(conn, src, tmp_path)
    verdict = rtfm._freshness(conn, src)
    assert verdict.stale is False
    assert verdict.scan is not None
    assert "a.png" in [r.path.name for r in verdict.scan.reachable.kept]   # reached it
    assert verdict.scan.wanted == []                                       # wanted none of it
    result = rtfm._EMPTY_INDEXED._replace(reachable=verdict.scan.reachable,
                                          wanted=verdict.scan.wanted)
    assert any("NOTHING SELECTED" in m for m in rtfm.report_all(conn, src, result))


def test_a_non_utf8_file_fails_loudly_rather_than_indexing_mangled(home, tmp_path):
    (tmp_path / "cp1252.md").write_bytes(b"caf\xe9 keyword\n")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=tmp_path, ext_blocklist=frozenset())
    got = _index(conn, src, tmp_path)
    assert [p.position for p in got.handled.problems] == ["cp1252.md"]
    assert "utf-8" in got.handled.problems[0].reason.lower()
    row = conn.execute("SELECT extracted_ok, error FROM contents").fetchone()
    assert row[0] == 0 and row[1]


def test_no_replacement_character_reaches_the_index(home, tmp_path):
    (tmp_path / "b.md").write_bytes(b"\x00\x01\x02 keyword\n")
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s", type="dir", path=tmp_path, ext_blocklist=frozenset())
    _index(conn, src, tmp_path)
    texts = [r[0] for r in conn.execute("SELECT text FROM content_fts")]
    assert not any("�" in t for t in texts)


def test_errors_replace_is_gone_from_the_source():
    # Both ADRs state it is removed. A grep is the only assertion that stays true when
    # someone adds a fourth decode site.
    import pathlib
    assert 'errors="replace"' not in pathlib.Path(rtfm.__file__).read_text()


def test_nothing_selected_names_the_reachable_count_and_the_policy(home, tmp_path):
    # A manifest whose extension list matches nothing indexes zero files and, before this,
    # gave no reason (ADR 0015's opening example).
    d = tmp_path / "c"
    d.mkdir()
    for n in ("a.md", "b.md", "c.md"):
        (d / n).write_text("x")
    src = rtfm.Source(name="bazel", type="dir", path=d, ext_allowlist=frozenset({".nomatch"}))
    conn = rtfm.get_index_db()
    result = _index(conn, src, d)
    [msg] = rtfm.report_all(conn, src, result)
    assert "NOTHING SELECTED 'bazel'" in msg
    assert "3 path(s) reachable" in msg
    assert "ext_allowlist = .nomatch" in msg


def test_nothing_selected_names_the_paths_policy_too(home, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    src = rtfm.Source(name="s", type="dir", path=tmp_path, paths=("nope",),
                      ext_blocklist=frozenset())
    conn = rtfm.get_index_db()
    result = _index(conn, src, tmp_path)
    [msg] = rtfm.report_all(conn, src, result)
    assert "paths = nope" in msg


def test_nothing_selected_in_blocklist_mode_summarizes_defaults_by_count(home, tmp_path):
    # The effective blocklist is the user's list unioned with rtfm's ~70 defaults. Dumping
    # the whole union produced a 700-character wall the user could not act on and that
    # buried the `paths` clause (fix round 1: a real defect, not a style choice). Name what
    # the user wrote; summarize the rest by count, and never show a default extension the
    # user never declared.
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.png").write_bytes(b"x")          # blocked by rtfm's default set, not by the user
    src = rtfm.Source(name="s", type="dir", path=d, ext_blocklist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    result = _index(conn, src, d)
    [msg] = rtfm.report_all(conn, src, result)
    assert "ext_blocklist = .md" in msg
    assert f"rtfm's {len(rtfm.DEFAULT_EXT_BLOCKLIST)} default" in msg
    assert ".png" not in msg                 # the user never wrote it; the wall is gone


def test_nothing_selected_in_blocklist_mode_with_an_empty_user_list(home, tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.png").write_bytes(b"x")
    src = rtfm.Source(name="s", type="dir", path=d, ext_blocklist=frozenset())
    conn = rtfm.get_index_db()
    result = _index(conn, src, d)
    [msg] = rtfm.report_all(conn, src, result)
    assert "ext_blocklist = []" in msg
    assert f"rtfm's {len(rtfm.DEFAULT_EXT_BLOCKLIST)} default" in msg
    assert ".png" not in msg


def test_an_empty_source_directory_selects_silently(home, tmp_path):
    # An empty folder is not a manifest error — reachable.kept is empty too, which is step
    # 1's story (silence), never step 2's.
    d = tmp_path / "c"
    d.mkdir()
    src = rtfm.Source(name="s", type="dir", path=d, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    result = _index(conn, src, d)
    assert rtfm.report_all(conn, src, result) == []


def test_an_unreadable_root_reports_access_only_not_nothing_selected(home, tmp_path, unopenable):
    t = tmp_path / "c"
    unopenable(t, directory=True)
    src = rtfm.Source(name="s", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = rtfm.get_index_db()
    result = _index(conn, src, t)
    msgs = rtfm.report_all(conn, src, result)
    assert any("COULD NOT OPEN" in m for m in msgs)
    assert not any("NOTHING SELECTED" in m for m in msgs)
