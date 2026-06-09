# tests/test_search_quality.py — Tier 1: doc-level signal (title/headings) + FTS5 parity
import fitz

import rtfm_server as rtfm


def _titled_pdf(path, title, toc_titles, pages):
    doc = fitz.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    if title is not None:
        doc.set_metadata({"title": title})
    if toc_titles:
        doc.set_toc([[1, t, 1] for t in toc_titles])
    doc.save(str(path))
    doc.close()


def test_pdf_title_and_headings_indexed(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    _titled_pdf(d / "spec.pdf", "Widget Protocol Specification",
                ["Introduction", "Flit Format"], ["alpha body text", "more text"])
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    row = conn.execute("SELECT title, headings FROM doc_fts").fetchone()
    assert row is not None
    assert "Widget Protocol Specification" in row[0]
    assert "Flit Format" in row[1]


def test_text_title_and_headings_indexed(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "guide.md").write_text("# Gadget Manual\n\nintro\n\n## Setup\nsteps\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    row = conn.execute("SELECT title, headings FROM doc_fts").fetchone()
    assert "Gadget Manual" in row[0]
    assert "Setup" in row[1]


def test_title_match_ranks_bearing_doc_first(home, tmp_path):
    """The doc whose TITLE matches the query outranks docs that merely mention the words in body
    — the core Tier 1 relevance win."""
    d = tmp_path / "docs"
    d.mkdir()
    # Bearing doc: title matches; body does NOT repeat the title terms.
    _titled_pdf(d / "bearer.pdf", "Flux Capacitor", [], ["This device enables time travel."])
    # Incidental doc: unrelated title; body mentions the terms several times.
    _titled_pdf(d / "incidental.pdf", "Misc Notes", [],
                ["flux capacitor here", "flux capacitor again", "flux capacitor more"])
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    hits = rtfm.search_index(conn, "flux capacitor")
    assert hits
    assert hits[0]["locations"][0]["relpath"] == "bearer.pdf"      # bearing doc ranks first
    assert "Flux Capacitor" in hits[0]["title"]
    assert hits[0]["locator_kind"] == "title"


def test_body_hit_carries_doc_title(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    _titled_pdf(d / "spec.pdf", "Widget Protocol Specification", [],
                ["the gizmo defines flits clearly"])
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    hits = rtfm.search_index(conn, "gizmo")                        # body-only match
    assert hits and "Widget Protocol Specification" in hits[0]["title"]


def test_fuzzy_marker_on_or_fallback(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("the kitchen has a sink on one line\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    precise = rtfm.search_index(conn, "kitchen sink")              # both terms co-occur -> AND
    assert precise and precise[0]["fuzzy"] is False
    fuzzy = rtfm.search_index(conn, "kitchen zephyr")              # zephyr absent: AND empty -> OR
    assert fuzzy and fuzzy[0]["fuzzy"] is True


def test_search_tool_flags_fuzzy(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("the kitchen has a sink\n")
    rtfm.load_manifest()
    rtfm.reindex(source=None)
    # put the doc in the default source so the tool searches it
    (rtfm.default_source_dir() / "a.md").write_text("the kitchen has a sink\n")
    out = rtfm.search(query="kitchen zephyr")
    assert out.get("fuzzy") is True


def test_snippet_picks_best_coverage_line(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text(
        "widget alone here\n"
        "the widget protocol together\n"          # the only line with BOTH terms
        "protocol alone here\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    hits = rtfm.search_index(conn, "widget protocol")
    assert "widget protocol together" in hits[0]["snippet"]
