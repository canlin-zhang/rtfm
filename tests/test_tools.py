# tests/test_tools.py
import rtfm_server as rt


def _seed(home, tmp_path):
    src_dir = tmp_path / "docs"
    src_dir.mkdir()
    (src_dir / "guide.md").write_text(
        "intro line\nthe widget protocol defines flits\nmore text\n")
    src = rt.Source(name="docs", type="dir", path=src_dir)
    conn = rt.get_index_db()
    rt.index_source(conn, src)
    return conn, src_dir


def test_search_returns_locator_hits(home, tmp_path):
    conn, _ = _seed(home, tmp_path)
    hits = rt.search_index(conn, "widget protocol")
    assert hits
    h = hits[0]
    assert h["source"] == "docs" and h["relpath"] == "guide.md"
    assert h["locator_kind"] == "line"
    assert "widget protocol" in h["snippet"]


def test_search_empty_query_errors(home, tmp_path):
    conn, _ = _seed(home, tmp_path)
    assert rt.search_index(conn, "   ") == []


def test_read_text_line_range(home, tmp_path):
    conn, src_dir = _seed(home, tmp_path)
    text = rt.read_document_text(rt.Source("docs", "dir", src_dir), "guide.md", 2, 2)
    assert "widget protocol" in text and "intro line" not in text


def test_search_malformed_query_does_not_raise(home, tmp_path):
    conn, _ = _seed(home, tmp_path)
    # Arbitrary punctuation must never raise; it degrades to keyword tokens.
    assert isinstance(rt.search_index(conn, "foo!bar("), list)
    assert isinstance(rt.search_index(conn, ")(*&^%$#@!"), list)
    # A punctuation-joined real term still finds the document.
    hits = rt.search_index(conn, "widget!protocol")
    assert any("widget protocol" in h["snippet"] for h in hits)


def test_list_sources_includes_default(home):
    out = rt.list_sources()
    assert any(s["name"] == "default" for s in out["sources"])


def test_health_check_reports_db_and_sources(home):
    out = rt.health_check()
    assert out["ok"] is True
    assert "default" in [s["name"] for s in out["sources"]]


def test_search_tool_end_to_end(home):
    # drop a file into the bootstrapped default source, then search via the tool
    rt.load_manifest()
    (rt.default_source_dir() / "n.md").write_text("zephyr keyword here\n")
    out = rt.search(query="zephyr")
    assert any("zephyr" in h["snippet"] for h in out["results"])
