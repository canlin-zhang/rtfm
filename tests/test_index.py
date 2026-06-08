# tests/test_index.py
import os
import sqlite3

import rtfm_server as rtfm


def test_schema_is_content_addressed(home):
    conn = rtfm.get_index_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rtfm.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contents", "locations"} <= tables
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
    assert "doc_meta" not in tables and "doc_fts" not in tables
    assert {"contents", "locations"} <= tables


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
