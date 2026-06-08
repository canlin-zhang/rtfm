# tests/test_index.py
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
    assert summary["deduped_skips"] == 1
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
    assert again["deduped_skips"] == 1 and again["newly_extracted"] == 0


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
