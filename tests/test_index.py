# tests/test_index.py
import rtfm_server as rt


def test_index_pdf_pages(home, sample_pdf):
    conn = rt.get_index_db()
    ok, err = rt.index_file(conn, "src", sample_pdf, sample_pdf.parent)
    assert ok, err
    rows = conn.execute(
        "SELECT locator_kind, locator_value, text FROM doc_fts WHERE source='src' ORDER BY locator_value"
    ).fetchall()
    assert [r[0] for r in rows] == ["page", "page"]
    assert rows[0][1] == "1" and "alpha" in rows[0][2]
    assert rows[1][1] == "2" and "foxtrot" in rows[1][2]


def test_index_text_lines(home, sample_txt):
    conn = rt.get_index_db()
    ok, err = rt.index_file(conn, "src", sample_txt, sample_txt.parent)
    assert ok, err
    rows = conn.execute(
        "SELECT locator_kind, locator_value FROM doc_fts WHERE source='src' ORDER BY CAST(locator_value AS INT)"
    ).fetchall()
    assert rows[0] == ("line", "1")
    assert rows[1] == ("line", "51")        # 50-line chunks


def test_unsupported_extension_skipped(home, tmp_path):
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n")
    conn = rt.get_index_db()
    ok, err = rt.index_file(conn, "src", img, tmp_path)
    assert ok is False and ".png" in err


def test_incremental_skips_unchanged(home, sample_txt):
    conn = rt.get_index_db()
    rt.index_file(conn, "src", sample_txt, sample_txt.parent)
    assert rt.ensure_indexed(conn, "src", sample_txt, sample_txt.parent) == "skipped"
    sample_txt.write_text("changed content\n")
    assert rt.ensure_indexed(conn, "src", sample_txt, sample_txt.parent) == "indexed"


def test_index_source_walks_supported_files(home, tmp_path):
    src_dir = tmp_path / "docs"
    src_dir.mkdir()
    (src_dir / "a.md").write_text("hello keyword\n")
    (src_dir / "b.png").write_bytes(b"\x89PNG")     # unsupported -> ignored
    sub = src_dir / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested keyword\n")
    src = rt.Source(name="docs", type="dir", path=src_dir)
    conn = rt.get_index_db()
    n = rt.index_source(conn, src)
    assert n == 2                                   # a.md + sub/c.txt
    files = {r[0] for r in conn.execute(
        "SELECT DISTINCT relpath FROM doc_meta WHERE source='docs'").fetchall()}
    assert files == {"a.md", "sub/c.txt"}


def test_purge_removes_deleted_files(home, tmp_path):
    src_dir = tmp_path / "docs"
    src_dir.mkdir()
    f = src_dir / "a.md"
    f.write_text("hello\n")
    src = rt.Source(name="docs", type="dir", path=src_dir)
    conn = rt.get_index_db()
    rt.index_source(conn, src)
    f.unlink()
    rt.index_source(conn, src)
    assert conn.execute(
        "SELECT COUNT(*) FROM doc_meta WHERE source='docs'").fetchone()[0] == 0
