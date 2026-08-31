"""web source type — validation, discovery, extraction, fetch, reindex, agent contract."""
import io
import sqlite3
import time
import urllib.error

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
    monkeypatch.setenv("RTFM_WEB_MAX_PAGES", "0")   # 0 is nonsense — reject to default
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


def test_web_url_parts_bare_version_segment():
    # A URL whose last path segment IS the version (no trailing slash, no file)
    # must scope to that version — NOT to its parent directory, or the
    # same-version filter would sweep every version of the site.
    assert rtfm._web_url_parts("https://slug.readthedocs.io/en/latest") == \
        ("https", "slug.readthedocs.io", "/en/latest/")
    assert rtfm._web_url_parts("https://docs.example.com/projects/widget/v2.18") == \
        ("https", "docs.example.com", "/projects/widget/v2.18/")


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


def test_html_to_text_void_elements_do_not_leak_depth():
    # Void elements (<img>/<br>/<hr>) emit no end tags — they must not push the
    # main-region depth, or chrome after </div role=main> leaks into the text.
    html = ('<html><body><div role="main" class="document"><h1>T</h1>'
            '<p>before<img src="x.png" alt="pic">after<br>line2</p><hr>'
            '</div><footer>FOOTER CHROME</footer></body></html>')
    title, headings, lines = rtfm._html_to_text(html)
    joined = "\n".join(lines)
    assert "FOOTER CHROME" not in joined
    assert "line2" in joined
    assert title == "T"


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


class _FakeResp:
    def __init__(self, body: bytes, code: int = 200):
        self.body = body
        self.code = code

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    """Subclasses the real exception so it flows through _http_get's except path."""

    def __init__(self, code: int, body: bytes = b""):
        super().__init__("http://x.example.com/", code, "msg", {}, io.BytesIO(body))


def _fake_urlopen(fail: _FakeHTTPError | None = None, body: bytes = b"ok"):
    def urlopen(req, **kwargs):
        if fail is not None:
            raise fail
        return _FakeResp(body)
    return urlopen


def test_http_get_success(monkeypatch):
    monkeypatch.setattr(rtfm, "_urlopen", _fake_urlopen(body=b"hello"))
    body, err = rtfm._http_get("https://x.example.com/")
    assert body == b"hello" and err is None


def test_http_get_rate_limited(monkeypatch):
    monkeypatch.setattr(rtfm, "_urlopen", _fake_urlopen(
        fail=_FakeHTTPError(429, b"slow down")))
    body, err = rtfm._http_get("https://x.example.com/")
    assert body is None and err.startswith("ERROR:RATE_LIMITED:")


def test_http_get_blocked_challenge_detected(monkeypatch):
    # Cloudflare walls come back 403 or 429 with a challenge body — the body wins.
    for code in (403, 429):
        monkeypatch.setattr(rtfm, "_urlopen", _fake_urlopen(
            fail=_FakeHTTPError(code, b"<title>Just a moment...</title> cf_chl x")))
        _, err = rtfm._http_get("https://x.example.com/")
        assert err is not None and err.startswith("ERROR:BLOCKED:"), code


def test_http_get_fetch_failed(monkeypatch):
    monkeypatch.setattr(rtfm, "_urlopen", _fake_urlopen(fail=_FakeHTTPError(404, b"nope")))
    _, err = rtfm._http_get("https://x.example.com/")
    assert err is not None and err.startswith("ERROR:FETCH_FAILED:")


def test_throttle_delay_constant(monkeypatch):
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)   # fast in tests
    rtfm._throttle()                                      # must not raise


def test_throttle_enforces_delay(monkeypatch):
    # The 1s politeness delay is load-bearing (RTD custom domains rate-limit) —
    # pin that _throttle actually sleeps, not just that it doesn't raise.
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.05)
    rtfm._throttle()
    t0 = time.monotonic()
    rtfm._throttle()
    assert time.monotonic() - t0 >= 0.05


def test_http_get_incomplete_read_fetch_failed(monkeypatch):
    # A truncated response (hostile CDN) must classify, not crash the tool call.
    import http.client

    def boom(req, **kwargs):
        raise http.client.IncompleteRead(b"partial")
    monkeypatch.setattr(rtfm, "_urlopen", boom)
    _, err = rtfm._http_get("https://x.example.com/")
    assert err is not None and err.startswith("ERROR:FETCH_FAILED:")


def test_reindex_tool_isolates_source_failures(home, monkeypatch):
    # One source's reindex raising must not abort the others (ADR 0014: one bad
    # source never breaks the rest).
    rtfm.manifest_path().write_text(
        f'[[source]]\nname="default"\ntype="dir"\npath="{rtfm.default_source_dir()}"\n'
        '[[source]]\nname="widget"\ntype="web"\nflavor="readthedocs"\n'
        'url="https://docs.example.com/projects/widget/latest/index.html"\n')

    def boom(url):
        raise RuntimeError("network exploded")
    monkeypatch.setattr(rtfm, "_fetch_page", boom)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    out = rtfm.reindex()   # all sources: default dir (ok) + widget (raises)
    entries = {e.get("source"): e for e in out["reindexed"]}
    assert "error" in entries["widget"]
    assert "error" not in entries["default"]


def test_html_hrefs_extracts_all():
    hrefs = rtfm._html_hrefs(RTD_INDEX)
    assert "tutorial/index.html" in hrefs and "guide.html" in hrefs


def test_llms_links_parses_markdown():
    assert rtfm._llms_links("[Intro](index.html)\n[Guide](guide.html)") == \
        ["index.html", "guide.html"]


INDEX_URL = "https://docs.example.com/projects/widget/latest/index.html"


def test_web_pages_filters_same_version():
    hrefs = ["tutorial/index.html", "guide.html", "search.html",
             "https://docs.example.com/projects/widget/v1.0/old.html",
             "https://elsewhere.example.com/x.html",
             "_static/theme.css", "genindex.html", "index.html",
             "tutorial/"]
    pages = rtfm._web_pages(INDEX_URL, hrefs)
    assert pages == ["guide.html", "index.html", "tutorial/index.html"]


def test_web_pages_normalizes_and_dedupes():
    pages = rtfm._web_pages(INDEX_URL, ["tutorial/", "tutorial/index.html", "tutorial"])
    assert pages == ["tutorial/index.html"]


def _fake_fetch(pages: dict[str, str], errors: dict[str, str] | None = None) -> callable:
    """_fetch_page-shaped fake. `pages` maps url → body; `errors` maps url → a
    classified error string (simulating what the real fetch layer returns for
    BLOCKED/RATE_LIMITED/etc.). `errors` is read at call time, so tests can
    mutate it between runs. `fail_after` maps url → call number (counted from
    when the url enters the map) at which fetches start failing — used to make
    a URL succeed during discovery but fail when the crawl re-fetches it."""
    calls: list[str] = []
    errors = errors or {}
    fail_after: dict[str, int] = {}
    fcounts: dict[str, int] = {}

    def fetch(url: str):
        calls.append(url)
        if url in fail_after:
            fcounts[url] = fcounts.get(url, 0) + 1
            if fcounts[url] >= fail_after[url]:
                return None, "ERROR:FETCH_FAILED: fail_after " + url
        if url in errors:
            return None, errors[url]
        body = pages.get(url)
        if body is None:
            return None, "ERROR:FETCH_FAILED: no fixture for " + url
        return body.encode(), None

    fetch.calls = calls
    fetch.pages = pages        # exposed so tests can mutate the maps between runs
    fetch.errors = errors
    fetch.fail_after = fail_after
    return fetch


def test_discover_llms_txt_fast_path():
    fetch = _fake_fetch({
        "https://docs.example.com/projects/widget/latest/llms.txt":
            "# Widget docs\n\n[Guide](guide.html)\n[Intro](index.html)\n",
        INDEX_URL: RTD_INDEX,   # the fast path still shape-checks the index page
    })
    pages, err = rtfm._web_discover(fetch, INDEX_URL)
    assert err is None
    assert pages == ["guide.html", "index.html"]
    assert fetch.calls == ["https://docs.example.com/projects/widget/latest/llms.txt",
                           INDEX_URL]


def test_discover_llms_txt_still_shape_checks_index():
    # A non-RTD site publishing llms.txt must not be indexed: the fast path cannot
    # bypass the NOT_READTHEDOCS guard.
    fetch = _fake_fetch({
        "https://docs.example.com/projects/widget/latest/llms.txt":
            "# Not docs\n\n[Guide](guide.html)\n",
        INDEX_URL: "<html><body><p>not docs at all</p></body></html>",
    })
    pages, err = rtfm._web_discover(fetch, INDEX_URL)
    assert pages == []
    assert err is not None and err.startswith("ERROR:NOT_READTHEDOCS:")


def test_discover_falls_back_to_nav_crawl():
    fetch = _fake_fetch({INDEX_URL: RTD_INDEX})
    pages, err = rtfm._web_discover(fetch, INDEX_URL)
    assert err is None
    assert pages == ["guide.html", "tutorial/index.html"]   # seed itself excluded


def test_discover_not_readthedocs_marks_failure():
    fetch = _fake_fetch({INDEX_URL: "<html><body><p>not docs at all</p></body></html>"})
    pages, err = rtfm._web_discover(fetch, INDEX_URL)
    assert pages == []
    assert err is not None and err.startswith("ERROR:NOT_READTHEDOCS:")


def test_discover_llms_404_falls_back_to_crawl():
    fetch = _fake_fetch({
        INDEX_URL: RTD_INDEX,
        "https://docs.example.com/projects/widget/latest/llms.txt": "404 page",
    })
    pages, err = rtfm._web_discover(fetch, INDEX_URL)
    assert err is None
    assert pages == ["guide.html", "tutorial/index.html"]


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://docs.example.com/projects/widget/latest/</loc>
<lastmod>2026-08-26T14:54:05+00:00</lastmod></url>
<url><loc>https://docs.example.com/projects/widget/stable/</loc>
<lastmod>2026-07-01T00:00:00+00:00</lastmod></url>
</urlset>"""

PAGE_TUT = """<html><head><title>Tutorial</title></head><body>
<div role="main" class="document"><h1>Tutorial</h1>
<p>The widget tutorial covers flits deeply.</p></div></body></html>"""


def test_sitemap_lastmod_matches_version_root():
    assert rtfm._sitemap_lastmod(SITEMAP.encode(), "/projects/widget/latest/") == \
        "2026-08-26T14:54:05+00:00"
    assert rtfm._sitemap_lastmod(SITEMAP.encode(), "/projects/widget/stable/") == \
        "2026-07-01T00:00:00+00:00"
    assert rtfm._sitemap_lastmod(SITEMAP.encode(), "/projects/widget/v1.0/") is None
    assert rtfm._sitemap_lastmod(b"<html>custom sitemap</html>", "/projects/widget/latest/") \
        is None


def _web_source(name="widget"):
    return rtfm.Source(name=name, type="web", flavor="readthedocs",
                       url="https://docs.example.com/projects/widget/latest/index.html")


def _manifest_with_widget() -> None:
    """Declare the widget web source in the manifest — needed by the tool-level
    tests (search/list_sources/reindex load the manifest; reindex_source does not)."""
    rtfm.manifest_path().write_text(
        '[[source]]\nname="widget"\ntype="web"\nflavor="readthedocs"\n'
        'url="https://docs.example.com/projects/widget/latest/index.html"\n')


def _full_fetch():
    """A fetch map for a complete small site: sitemap + index + 2 pages."""
    idx = "https://docs.example.com/projects/widget/latest/index.html"
    sitemap_url = "https://docs.example.com/sitemap.xml"
    return _fake_fetch({
        sitemap_url: SITEMAP,
        idx: RTD_INDEX,
        "https://docs.example.com/projects/widget/latest/guide.html": RTD_PAGE,
        "https://docs.example.com/projects/widget/latest/tutorial/index.html": PAGE_TUT,
    })


def test_reindex_web_happy_path(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    out = rtfm.reindex_source(conn, _web_source())
    assert "error" not in out, out
    assert out["pages_fetched"] == 3
    cache = rtfm._web_cache_path("widget")
    assert (cache / "index.html").exists()
    assert (cache / "guide.html").exists()
    assert (cache / "tutorial" / "index.html").exists()
    hits = rtfm.search_index(conn, "flits")
    assert any(h["title"] == "Guide" for h in hits)   # both pages mention flits; order not fixed
    meta = rtfm._web_meta(conn, "widget")
    assert meta["status"] == "ok"
    assert meta["page_count"] == 3
    assert meta["lastmod"] == "2026-08-26T14:54:05+00:00"
    assert meta["version"] == "latest"


def test_reindex_web_skips_when_lastmod_unchanged(home, monkeypatch):
    # First run indexes the site; second run must fetch nothing but its sitemap
    # probe (same lastmod + previous run completed → skip the crawl).
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    after_run1 = len(fetch.calls)
    out = rtfm.reindex_source(conn, _web_source())
    assert out["status"] == "up to date"
    assert len(fetch.calls) == after_run1 + 1    # exactly one more call: the probe


def test_reindex_web_recrawls_when_lastmod_changed(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    fetch.pages["https://docs.example.com/sitemap.xml"] = \
        SITEMAP.replace("2026-08-26", "2026-09-01")
    out = rtfm.reindex_source(conn, _web_source())
    assert out["pages_fetched"] == 3


def test_reindex_web_truncated_by_cap(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    monkeypatch.setenv("RTFM_WEB_MAX_PAGES", "2")
    conn = rtfm.get_index_db()
    out = rtfm.reindex_source(conn, _web_source())
    assert out["truncated"] is True
    assert out["total_pages"] == 3
    meta = rtfm._web_meta(conn, "widget")
    assert meta["status"] == "truncated"
    assert meta["page_count"] == 2


def test_reindex_web_blocked_aborts(home, monkeypatch):
    # The index URL's fetch returns the CLASSIFIED error (what the real fetch
    # layer produces for a Cloudflare wall) — discovery must surface it as a
    # failed reindex, not a crawl of the challenge page.
    fetch = _fake_fetch(
        {"https://docs.example.com/sitemap.xml": SITEMAP},
        errors={"https://docs.example.com/projects/widget/latest/index.html":
                "ERROR:BLOCKED: bot-protection challenge"},
    )
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    out = rtfm.reindex_source(conn, _web_source())
    assert "error" in out and out["error"].startswith("ERROR:BLOCKED:")
    meta = rtfm._web_meta(conn, "widget")
    assert meta is not None and meta["status"] == "error"
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='widget'") \
        .fetchone()[0] == 0


def test_reindex_web_not_readthedocs(home, monkeypatch):
    fetch = _fake_fetch({
        "https://docs.example.com/sitemap.xml": SITEMAP,
        "https://docs.example.com/projects/widget/latest/index.html":
            "<html><body><p>not docs</p></body></html>"})
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    out = rtfm.reindex_source(conn, _web_source())
    assert out["error"].startswith("ERROR:NOT_READTHEDOCS:")


def test_reindex_web_read_from_cache(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    text = rtfm.read_document_text(_web_source(), "guide.html", 1, 5)
    assert "flits" in text and "<html>" not in text


def test_reindex_web_partial_failure_preserves_content(home, monkeypatch):
    # A page that fails mid-crawl (FETCH_FAILED) must NOT lose its prior content:
    # the cache file and index rows stay, the run records the failure loudly, and
    # a later skip must not lock the loss in.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    fetch.errors["https://docs.example.com/projects/widget/latest/tutorial/index.html"] = \
        "ERROR:FETCH_FAILED: HTTP 500"
    fetch.pages["https://docs.example.com/sitemap.xml"] = \
        SITEMAP.replace("2026-08-26", "2026-09-01")
    out = rtfm.reindex_source(conn, _web_source())
    assert out["pages_failed"] == 1
    cache = rtfm._web_cache_path("widget")
    assert (cache / "tutorial" / "index.html").exists()   # prior file preserved
    assert any(h["title"] == "Tutorial" for h in rtfm.search_index(conn, "tutorial"))
    meta = rtfm._web_meta(conn, "widget")
    assert meta["status"] == "error"
    assert "1 of 3 pages failed" in meta["error"]


def test_reindex_web_failed_run_never_skips(home, monkeypatch):
    # After a partial-failure run, the skip gate must NOT fire even when the sitemap
    # lastmod later matches again — the loss must not be locked in silently. A later
    # clean run recovers to 'ok'.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    fetch.errors["https://docs.example.com/projects/widget/latest/tutorial/index.html"] = \
        "ERROR:FETCH_FAILED: HTTP 500"
    fetch.pages["https://docs.example.com/sitemap.xml"] = \
        SITEMAP.replace("2026-08-26", "2026-09-01")
    rtfm.reindex_source(conn, _web_source())          # run 2: partial failure
    del fetch.errors["https://docs.example.com/projects/widget/latest/tutorial/index.html"]
    out = rtfm.reindex_source(conn, _web_source())    # run 3: clean again
    assert out.get("pages_fetched") == 3              # crawled — never "up to date"
    assert rtfm._web_meta(conn, "widget")["status"] == "ok"


def test_reindex_web_bare_url_fetches_from_version_root(home, monkeypatch):
    # A bare entry URL ('.../latest' with no trailing slash) must fetch pages from
    # the VERSION ROOT — urljoin against the raw URL treats 'latest' as a file and
    # drops the version segment, 404ing every page fetch.
    src = rtfm.Source(name="widget", type="web", flavor="readthedocs",
                      url="https://docs.example.com/projects/widget/latest")
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    out = rtfm.reindex_source(conn, src)
    assert "error" not in out, out
    assert out["pages_fetched"] == 3
    assert any(c.endswith("/projects/widget/latest/guide.html") for c in fetch.calls)
    # the parent-scope URL (version segment dropped) must never be fetched
    assert not any(c.endswith("/projects/widget/guide.html") for c in fetch.calls)


def test_reindex_web_skip_gate_checks_url_identity(home, monkeypatch):
    # Same source name, DIFFERENT url, same lastmod → the skip gate must not fire:
    # 'up to date' while serving the old URL's content is the silent-loss mode.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    other = rtfm.Source(name="widget", type="web", flavor="readthedocs",
                        url="https://docs.example.com/projects/widget/latest/stable.html")
    out = rtfm.reindex_source(conn, other)
    assert out.get("pages_fetched") == 3          # crawled — not "up to date"


def test_reindex_web_crash_routes_to_failed_state(home, monkeypatch):
    # A crash mid-reindex (disk full, sqlite, extraction) must land in web_meta as
    # 'error' — swallowed by the tool wrapper with the old 'ok' standing is the
    # healthy-while-degraded failure mode.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())      # run 1: ok

    def boom(url):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rtfm, "_fetch_page", boom)
    out = rtfm.reindex_source(conn, _web_source())
    assert "error" in out and "disk full" in out["error"]
    meta = rtfm._web_meta(conn, "widget")
    assert meta["status"] == "error"
    assert any("SOURCE FAILED" in w for w in rtfm.search(query="widget protocol")
               .get("WARNING", []))


def test_reindex_web_aborts_on_page_blocked(home, monkeypatch):
    # A host-level wall hit MID-CRAWL (a page, not the index) must abort with the
    # classified error — dead code to the suite until this pins it.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    fetch.errors["https://docs.example.com/projects/widget/latest/guide.html"] = \
        "ERROR:BLOCKED: bot-protection challenge"
    out = rtfm.reindex_source(conn, _web_source())
    assert out["error"].startswith("ERROR:BLOCKED:")
    assert rtfm._web_meta(conn, "widget")["status"] == "error"


def test_reindex_web_all_targets_fail_after_discovery(home, monkeypatch):
    # Discovery succeeds; every TARGET page fails (index fails on its second
    # fetch — the crawl re-fetches what discovery already saw). Prior content is
    # preserved and the run is 'error', never 'ok'.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    # in-place mutation — the fake's closure reads the original dicts
    fetch.errors["https://docs.example.com/projects/widget/latest/guide.html"] = \
        "ERROR:FETCH_FAILED: HTTP 500"
    fetch.errors["https://docs.example.com/projects/widget/latest/tutorial/index.html"] = \
        "ERROR:FETCH_FAILED: HTTP 500"
    fetch.fail_after["https://docs.example.com/projects/widget/latest/index.html"] = 2
    fetch.pages["https://docs.example.com/sitemap.xml"] = \
        SITEMAP.replace("2026-08-26", "2026-09-01")
    out = rtfm.reindex_source(conn, _web_source())
    assert out["pages_failed"] == 3
    assert rtfm._web_meta(conn, "widget")["status"] == "error"
    assert any(h["title"] == "Guide" for h in rtfm.search_index(conn, "flits"))


def test_reindex_web_total_failure_preserves_prior(home, monkeypatch):
    # Every fetch fails (the host is down): discovery itself fails, _web_fail
    # records the error with the PRIOR counts preserved, and prior content stays
    # searchable — list_sources must never report 'indexed' with page_count 0.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    for url in list(fetch.pages):
        fetch.errors[url] = "ERROR:FETCH_FAILED: host down"
    out = rtfm.reindex_source(conn, _web_source())
    assert "error" in out and out["error"].startswith("ERROR:FETCH_FAILED:")
    meta = rtfm._web_meta(conn, "widget")
    assert meta["status"] == "error"
    assert meta["page_count"] == 3                    # prior count preserved, not zeroed
    assert any(h["title"] == "Guide" for h in rtfm.search_index(conn, "flits"))


def test_reindex_web_purges_pages_gone_upstream(home, monkeypatch):
    # A page whose link vanished upstream (no longer discovered) leaves the cache
    # and its index row — that purge is the genuine case, distinct from a fetch
    # failure.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    fetch.pages["https://docs.example.com/projects/widget/latest/index.html"] = \
        RTD_INDEX.replace("tutorial/index.html", "")  # link removed upstream
    fetch.pages["https://docs.example.com/sitemap.xml"] = \
        SITEMAP.replace("2026-08-26", "2026-09-01")
    out = rtfm.reindex_source(conn, _web_source())
    assert out["purged"] == 1
    assert not (rtfm._web_cache_path("widget") / "tutorial" / "index.html").exists()


def test_search_sources_failed_lists_never_indexed_web(home, monkeypatch):
    rtfm.manifest_path().write_text(
        '[[source]]\nname="w"\ntype="web"\nflavor="readthedocs"\n'
        'url="https://docs.example.com/projects/widget/latest/index.html"\n')
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    out = rtfm.search(query="widget protocol")
    failed = {s["name"]: s["state"] for s in out.get("sources_failed", [])}
    assert failed.get("w") == "never indexed"
    assert "w" not in out["sources_searched"]


def test_search_sources_failed_lists_failed_web(home, monkeypatch):
    fetch = _fake_fetch(
        {"https://docs.example.com/sitemap.xml": SITEMAP},
        errors={"https://docs.example.com/projects/widget/latest/index.html":
                "ERROR:BLOCKED: bot-protection challenge"})
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    out = rtfm.search(query="widget protocol")
    failed = {s["name"]: s["state"] for s in out.get("sources_failed", [])}
    assert "last index FAILED" in failed.get("widget", "") and "BLOCKED" in failed["widget"]
    assert "widget" not in out["sources_searched"]


def test_search_indexed_web_in_sources_searched(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    out = rtfm.search(query="widget protocol")
    assert "widget" in out["sources_searched"]
    assert out.get("sources_failed") in (None, [])


def test_search_warns_when_failed_reindex_leaves_content(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    # Second run fails at the host level; prior content stays searchable, and
    # search must say so loudly.
    fetch.errors["https://docs.example.com/projects/widget/latest/index.html"] = \
        "ERROR:BLOCKED: bot-protection challenge"
    fetch.pages["https://docs.example.com/sitemap.xml"] = \
        SITEMAP.replace("2026-08-26", "2026-09-01")
    rtfm.reindex_source(conn, _web_source())
    out = rtfm.search(query="widget protocol")
    assert any("SOURCE FAILED" in w and "BLOCKED" in w for w in out.get("WARNING", []))
    assert "widget" in out["sources_searched"]          # prior content still searched


def test_list_sources_web_status(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    out = rtfm.list_sources()
    item = next(i for i in out["sources"] if i["name"] == "widget")
    assert item["web_status"] == "indexed"
    assert item["tracking_version"] == "latest"
    assert item["page_count"] == 3
    assert item["url"] == "https://docs.example.com/projects/widget/latest/index.html"
    assert item["flavor"] == "readthedocs"


def test_list_sources_web_status_never_indexed(home):
    rtfm.manifest_path().write_text(
        '[[source]]\nname="w"\ntype="web"\nflavor="readthedocs"\n'
        'url="https://docs.example.com/en/latest/index.html"\n')
    out = rtfm.list_sources()
    item = next(i for i in out["sources"] if i["name"] == "w")
    assert item["web_status"] == "never indexed"


def test_reindex_tool_targets_web(home, monkeypatch):
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    rtfm.manifest_path().write_text(
        '[[source]]\nname="widget"\ntype="web"\nflavor="readthedocs"\n'
        'url="https://docs.example.com/projects/widget/latest/index.html"\n')
    out = rtfm.reindex(source="widget")
    assert any(r.get("pages_fetched") == 3 for r in out["reindexed"])


def test_search_warns_on_truncated_source(home, monkeypatch):
    # A truncated corpus is partial coverage — search must say so, or an agent
    # reads a 2000-of-3500-page corpus as fully covered.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    monkeypatch.setenv("RTFM_WEB_MAX_PAGES", "2")
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    out = rtfm.search(query="widget protocol")
    assert any("SOURCE TRUNCATED" in w and "2 of 3" in w for w in out.get("WARNING", []))
    assert "widget" in out["sources_searched"]


def test_reindex_purges_web_meta_on_source_drop(home, monkeypatch):
    # A dropped source must lose its web_meta row too — otherwise re-adding it
    # reports a stale 'indexed' while search says 'never indexed'.
    fetch = _full_fetch()
    monkeypatch.setattr(rtfm, "_fetch_page", fetch)
    monkeypatch.setattr(rtfm, "_WEB_FETCH_DELAY", 0.0)
    _manifest_with_widget()
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, _web_source())
    assert rtfm._web_meta(conn, "widget") is not None
    rtfm.manifest_path().write_text(
        f'[[source]]\nname="default"\ntype="dir"\npath="{rtfm.default_source_dir()}"\n')
    out = rtfm.reindex()
    assert "widget" in out.get("purged_sources", [])
    assert rtfm._web_meta(conn, "widget") is None


def test_migrate_schema_drops_web_meta(home):
    conn = rtfm.get_index_db()
    rtfm._web_meta_write(conn, _web_source(), version="latest", fetched_at=1.0,
                         page_count=1, total_pages=1, lastmod=None, status="ok", error=None)
    rtfm._migrate_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM web_meta").fetchone()[0] == 0


def test_web_source_with_path_warns(home):
    # A web source ignores 'path' — saying so loudly beats silent divergence
    # between read (cache) and what the user expected (their path).
    _manifest_with('[[source]]\nname="w"\ntype="web"\nflavor="readthedocs"\n'
                   'url="https://x.example.com/en/latest/index.html"\npath="/tmp/x"\n')
    _, warnings = rtfm.load_manifest()
    assert any("ignore 'path'" in w for w in warnings)


def test_sitemap_lastmod_matches_index_html_loc():
    xml = ('<urlset><url><loc>https://docs.example.com/projects/widget/latest/index.html</loc>'
           '<lastmod>2026-08-26T00:00:00+00:00</lastmod></url></urlset>')
    assert rtfm._sitemap_lastmod(xml.encode(), "/projects/widget/latest/") == \
        "2026-08-26T00:00:00+00:00"


def test_web_meta_rejects_unknown_status(home):
    conn = rtfm.get_index_db()
    with pytest.raises(sqlite3.IntegrityError):
        rtfm._web_meta_write(conn, _web_source(), version="latest", fetched_at=1.0,
                             page_count=1, total_pages=1, lastmod=None,
                             status="bogus", error=None)
