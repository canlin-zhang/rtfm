"""web source type — validation, discovery, extraction, fetch, reindex, agent contract."""
import pytest

import rtfm_server as rtfm


def _manifest_with(table: str) -> None:
    rtfm.manifest_path().write_text(table)


def test_web_source_requires_url(home):
    _manifest_with('[[source]]\nname="w"\ntype="web"\nflavor="readthedocs"\n')
    sources, warnings = rtfm.load_manifest()
    assert [s.name for s in sources] == []
    assert any("web source has no 'url'" in w for w in warnings)


def test_web_source_requires_flavor_kept_but_warned(home):
    # A flavor-less web source is loud but kept (self-heals when fixed) — the same
    # keep-but-warn semantics as a dir source with a missing path.
    _manifest_with('[[source]]\nname="w"\ntype="web"\nurl="https://x.example.com/en/latest/index.html"\n')
    sources, warnings = rtfm.load_manifest()
    assert [s.name for s in sources] == ["w"]
    assert any("has no 'flavor'" in w for w in warnings)


def test_web_source_unknown_flavor_kept_but_warned(home):
    _manifest_with('[[source]]\nname="w"\ntype="web"\nflavor="gitbook"\n'
                   'url="https://x.example.com/en/latest/index.html"\n')
    sources, warnings = rtfm.load_manifest()
    assert [s.name for s in sources] == ["w"]
    assert any("unknown flavor 'gitbook'" in w for w in warnings)


def test_web_source_valid_entry_parses_flavor(home):
    _manifest_with('[[source]]\nname="w"\ntype="web"\nflavor="readthedocs"\n'
                   'url="https://x.example.com/en/latest/index.html"\n')
    sources, warnings = rtfm.load_manifest()
    assert [s.name for s in sources] == ["w"]
    assert sources[0].flavor == "readthedocs"
    assert sources[0].url == "https://x.example.com/en/latest/index.html"
    assert not warnings


def test_unknown_type_message_mentions_web(home):
    _manifest_with('[[source]]\nname="w"\ntype="wobble"\n')
    _, warnings = rtfm.load_manifest()
    assert any("expected 'dir', 'git_repo', or 'web'" in w for w in warnings)


def test_web_cache_path(home):
    assert str(rtfm._web_cache_path("ansible")).endswith("web/ansible")


def test_web_max_pages_default_and_env(monkeypatch):
    assert rtfm._web_max_pages() == 2000
    monkeypatch.setenv("RTFM_WEB_MAX_PAGES", "42")
    assert rtfm._web_max_pages() == 42
    monkeypatch.setenv("RTFM_WEB_MAX_PAGES", "nope")
    assert rtfm._web_max_pages() == 2000


def test_web_url_parts_three_shapes():
    assert rtfm._web_url_parts("https://slug.readthedocs.io/en/latest/index.html") == \
        ("https", "slug.readthedocs.io", "/en/latest/")
    assert rtfm._web_url_parts("https://docs.ansible.com/projects/ansible/latest/index.html") == \
        ("https", "docs.ansible.com", "/projects/ansible/latest/")
    assert rtfm._web_url_parts("https://docs.example.com/en/stable/") == \
        ("https", "docs.example.com", "/en/stable/")


def test_web_url_parts_rejects_non_http(home):
    with pytest.raises(ValueError):
        rtfm._web_url_parts("file:///tmp/x.html")
    with pytest.raises(ValueError):
        rtfm._web_url_parts("not a url")


RTD_INDEX = """<!DOCTYPE html><html><head><title>Widget docs &mdash; Project docs</title></head>
<body class="wy-body-for-nav"><div class="wy-grid-for-nav"><nav class="wy-nav-side">
<div class="wy-menu wy-menu-vertical"><ul>
<li><a href="tutorial/index.html">Tutorial</a></li>
<li><a href="guide.html">Guide</a></li>
<li><a href="https://docs.example.com/projects/widget/v1.0/old.html">Old version</a></li>
<li><a href="https://elsewhere.example.com/x.html">Elsewhere</a></li>
<li><a href="search.html">Search</a></li>
</ul></div></nav>
<div class="wy-nav-content"><div class="rst-content">
<div role="main" class="document"><h1>Widget docs</h1>
<p>Widgets define the widget protocol.</p>
<h2>Usage</h2><p>Run <code>widget run</code>.</p>
<pre>widget init --force
widget run</pre>
<script>var x = "not searchable";</script>
</div></div></div></body></html>"""

RTD_PAGE = """<html><head><title>Guide</title></head><body>
<div class="wy-nav-side"><nav><a href="index.html">Home</a></nav></div>
<div role="main" class="document"><h1>Guide</h1>
<p>The widget protocol defines flits.</p>
<h3>Sub</h3><p>Detail here.</p></div></body></html>"""


def test_html_to_text_extracts_main_only():
    title, headings, lines = rtfm._html_to_text(RTD_PAGE)
    assert title == "Guide"
    assert "flits" in " ".join(lines)
    assert "Home" not in " ".join(lines)          # nav chrome dropped
    assert "Sub" in headings and "Guide" in headings


def test_html_to_text_keeps_code_verbatim():
    title, headings, lines = rtfm._html_to_text(RTD_INDEX)
    joined = "\n".join(lines)
    assert "widget init --force" in joined        # <pre> verbatim, not collapsed
    assert "widget run" in joined
    assert "not searchable" not in joined         # <script> dropped
    assert "Old version" not in joined            # nav dropped


def test_html_to_text_no_main_region():
    assert rtfm._html_to_text("<html><body><p>plain</p></body></html>") == ("", "", [])


def test_html_rows_chunk_lines(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "guide.html").write_text(RTD_PAGE)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d))
    hits = rtfm.search_index(conn, "flits")
    assert hits and hits[0]["title"] == "Guide"
    assert hits[0]["locator_kind"] == "line"


def test_read_html_returns_extracted_text_not_raw(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "guide.html").write_text(RTD_PAGE)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d))
    text = rtfm.read_document_text(rtfm.Source("docs", "dir", d), "guide.html", 1, 5)
    assert "flits" in text
    assert "<html>" not in text                  # extracted text, not raw markup
