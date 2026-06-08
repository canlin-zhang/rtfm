# tests/test_tools.py
import rtfm_server as rtfm


def _seed(home, tmp_path, name="docs"):
    d = tmp_path / name
    d.mkdir()
    (d / "guide.md").write_text("intro line\nthe widget protocol defines flits\nmore text\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name=name, type="dir", path=d))
    return conn, d


def _add_source(name, path):
    mp = rtfm.manifest_path()
    mp.write_text(mp.read_text() + f'\n[[source]]\nname="{name}"\ntype="dir"\npath="{path}"\n')


def test_search_hit_shape_and_locations(home, tmp_path):
    conn, _ = _seed(home, tmp_path)
    hits = rtfm.search_index(conn, "widget protocol")
    assert hits
    h = hits[0]
    assert h["locator_kind"] == "line"
    assert "widget protocol" in h["snippet"]
    assert h["locations"] == [{"source": "docs", "relpath": "guide.md"}]
    assert h["total_locations"] == 1


def test_search_dedup_lists_all_paths(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    body = "the widget protocol defines flits\n"
    for n in ("a.md", "b.md", "c.md"):
        (d / n).write_text(body)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    hits = rtfm.search_index(conn, "widget protocol")
    assert len(hits) == 1  # one content, not three
    assert hits[0]["total_locations"] == 3
    assert {loc["relpath"] for loc in hits[0]["locations"]} == {"a.md", "b.md", "c.md"}


def test_search_caps_locations(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    body = "the widget protocol defines flits\n"
    for i in range(7):
        (d / f"f{i}.md").write_text(body)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    hits = rtfm.search_index(conn, "widget protocol", max_locations=5)
    assert len(hits[0]["locations"]) == 5
    assert hits[0]["total_locations"] == 7


def test_search_empty_query_errors(home, tmp_path):
    conn, _ = _seed(home, tmp_path)
    assert rtfm.search_index(conn, "   ") == []


def test_search_malformed_query_does_not_raise(home, tmp_path):
    conn, _ = _seed(home, tmp_path)
    assert isinstance(rtfm.search_index(conn, ")(*&^%$#@!"), list)
    hits = rtfm.search_index(conn, "widget!protocol")
    assert any("widget protocol" in h["snippet"] for h in hits)


def test_read_text_line_range(home, tmp_path):
    conn, d = _seed(home, tmp_path)
    text = rtfm.read_document_text(rtfm.Source("docs", "dir", d), "guide.md", 2, 2)
    assert "widget protocol" in text and "intro line" not in text


def test_search_is_query_only_and_warns_on_unindexed(home, tmp_path):
    d = tmp_path / "manual"
    d.mkdir()
    (d / "m.md").write_text("the widget protocol defines flits\n")
    rtfm.load_manifest()  # bootstrap default
    _add_source("manual", d)  # configured but NOT reindexed
    out = rtfm.search(query="widget protocol", source="manual")
    assert out["results"] == []
    assert "WARNING" in out and any("NOT INDEXED" in w for w in out["WARNING"])
    conn = rtfm.get_index_db()
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='manual'").fetchone()[0] == 0


def test_default_self_heals_on_search(home):
    rtfm.load_manifest()
    (rtfm.default_source_dir() / "n.md").write_text("zephyr keyword here\n")
    out = rtfm.search(query="zephyr")
    assert any("zephyr" in h["snippet"] for h in out["results"])


def test_reindex_tool_returns_summary(home, tmp_path):
    d = tmp_path / "manual"
    d.mkdir()
    (d / "m.md").write_text("widget protocol\n")
    rtfm.load_manifest()
    _add_source("manual", d)
    out = rtfm.reindex(source="manual")
    assert out["reindexed"][0]["source"] == "manual"
    assert out["reindexed"][0]["files_seen"] == 1


def test_list_sources_reports_counts(home):
    out = rtfm.list_sources()
    assert any(s["name"] == "default" for s in out["sources"])
    assert all("unique_contents" in s for s in out["sources"])


def test_health_check_reports_schema_version(home):
    out = rtfm.health_check()
    assert out["ok"] is True
    assert out["schema_version"] == rtfm.SCHEMA_VERSION


def test_source_filter_restricts_search(home, tmp_path):
    """source= filter on search_index returns only hits from that source."""
    # Index two sources with distinct, non-overlapping content.
    d_docs = tmp_path / "docs"
    d_docs.mkdir()
    (d_docs / "guide.md").write_text("alpha unique_one content here\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d_docs))

    d_manual = tmp_path / "manual"
    d_manual.mkdir()
    (d_manual / "ref.md").write_text("bravo unique_two content here\n")
    rtfm.load_manifest()  # bootstrap default so manifest exists
    _add_source("manual", d_manual)
    rtfm.reindex_source(conn, rtfm.Source(name="manual", type="dir", path=d_manual))

    # unique_two is in manual, not docs.
    hits_manual = rtfm.search_index(conn, "unique_two", source="manual")
    assert hits_manual and any("unique_two" in h["snippet"] for h in hits_manual)

    hits_docs = rtfm.search_index(conn, "unique_two", source="docs")
    assert hits_docs == []
