# tests/test_tools.py
from conftest import make_git_repo

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


def test_search_auto_reindexes_small_unindexed_source(home, tmp_path):
    """A newly-added small source (within the auto-reindex budget) is indexed inline on search;
    the user no longer has to call reindex first. Phase 2 amends the old query-only behavior."""
    d = tmp_path / "manual"
    d.mkdir()
    (d / "m.md").write_text("the widget protocol defines flits\n")
    rtfm.load_manifest()  # bootstrap default
    _add_source("manual", d)  # configured but NOT reindexed
    out = rtfm.search(query="widget protocol", source="manual")
    assert any("widget protocol" in h["snippet"] for h in out["results"])
    conn = rtfm.get_index_db()
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='manual'").fetchone()[0] == 1


def test_search_warns_and_falls_back_on_large_unindexed_source(home, tmp_path, monkeypatch):
    """A source whose new/changed file count exceeds the budget is NOT reindexed inline (no
    blocking extraction): search serves prior content and warns to run reindex explicitly."""
    monkeypatch.setenv("RTFM_AUTO_REINDEX_MAX", "2")
    d = tmp_path / "big"
    d.mkdir()
    for i in range(3):  # 3 new files > budget of 2
        (d / f"f{i}.md").write_text("the widget protocol defines flits\n")
    rtfm.load_manifest()
    _add_source("big", d)
    out = rtfm.search(query="widget protocol", source="big")
    assert out["results"] == []
    assert "WARNING" in out and any("big" in w and "reindex" in w for w in out["WARNING"])
    conn = rtfm.get_index_db()
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='big'").fetchone()[0] == 0


def test_search_auto_reindexes_changed_file(home, tmp_path):
    """An already-indexed source whose file changed on disk is refreshed on the next search."""
    import os
    d = tmp_path / "manual"
    d.mkdir()
    f = d / "m.md"
    f.write_text("the gadget protocol is old\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("manual", "dir", d))
    rtfm.load_manifest()
    _add_source("manual", d)
    f.write_text("the widget protocol is new\n")          # changed content...
    os.utime(f, (1_900_000_000, 1_900_000_000))            # ...and a clearly newer mtime
    out = rtfm.search(query="widget protocol", source="manual")
    assert any("widget protocol" in h["snippet"] for h in out["results"])
    assert rtfm.search(query="gadget", source="manual")["results"] == []  # stale content gone


def test_search_survives_inline_reindex_failure(home, tmp_path, monkeypatch):
    """If an inline auto-reindex raises (file vanishes mid-rebuild, disk/lock error), search must
    degrade to a WARNING and still serve prior content — one source's refresh never fails the
    query, and never blocks it either."""
    d = tmp_path / "manual"
    d.mkdir()
    (d / "m.md").write_text("the widget protocol defines flits\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("manual", "dir", d))   # prior content indexed
    rtfm.load_manifest()
    _add_source("manual", d)
    (d / "new.md").write_text("more widget content\n")            # make it look stale...

    def boom(*a, **k):
        raise OSError("simulated mid-rebuild failure")
    monkeypatch.setattr(rtfm, "reindex_source", boom)             # ...then blow up the rebuild
    out = rtfm.search(query="widget protocol", source="manual")
    assert any("widget protocol" in h["snippet"] for h in out["results"])   # prior content served
    assert "WARNING" in out and any("AUTO-REINDEX FAILED" in w and "manual" in w
                                    for w in out["WARNING"])


def test_search_auto_reindex_at_budget_boundary_indexes(home, tmp_path, monkeypatch):
    """changed == budget is within budget (the <= boundary is inclusive): it reindexes inline.
    The strictly-over case (changed == budget+1 warns) is covered by the large-source test."""
    monkeypatch.setenv("RTFM_AUTO_REINDEX_MAX", "2")
    d = tmp_path / "edge"
    d.mkdir()
    (d / "a.md").write_text("widget alpha\n")
    (d / "b.md").write_text("widget bravo\n")                     # exactly 2 new == budget 2
    rtfm.load_manifest()
    _add_source("edge", d)
    out = rtfm.search(query="widget", source="edge")
    assert any("widget" in h["snippet"] for h in out["results"])
    conn = rtfm.get_index_db()
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE source='edge'").fetchone()[0] == 2


def test_search_purges_vanished_file_inline(home, tmp_path):
    """A file deleted on disk is purged on the next search (changed=0 but stale) with no warning —
    a free cleanup, never charged against the budget."""
    d = tmp_path / "manual"
    d.mkdir()
    (d / "keep.md").write_text("widget keeper\n")
    (d / "gone.md").write_text("widget goner\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("manual", "dir", d))
    rtfm.load_manifest()
    _add_source("manual", d)
    (d / "gone.md").unlink()                                      # vanish
    out = rtfm.search(query="goner", source="manual")
    assert out["results"] == []                                   # purged inline
    assert "WARNING" not in out                                   # free purge, no stale warning
    kept = rtfm.search(query="keeper", source="manual")
    assert any("keeper" in h["snippet"] for h in kept["results"])


def test_search_auto_reindex_disabled_with_zero_budget(home, tmp_path, monkeypatch):
    """RTFM_AUTO_REINDEX_MAX=0 disables inline auto-reindex entirely — even one new file only
    warns. The explicit, never-blocks escape hatch."""
    monkeypatch.setenv("RTFM_AUTO_REINDEX_MAX", "0")
    d = tmp_path / "manual"
    d.mkdir()
    (d / "m.md").write_text("the widget protocol defines flits\n")
    rtfm.load_manifest()
    _add_source("manual", d)
    out = rtfm.search(query="widget protocol", source="manual")
    assert out["results"] == []
    assert "WARNING" in out and any("manual" in w for w in out["WARNING"])


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


def test_list_sources_reports_git_status(home, tmp_path, git_branch):
    """list_sources reports git status for git_repo sources."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md", content="hello\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref=branch)
    rtfm.reindex_source(conn, src)
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\n'
        f'url="{remote}"\nref="{branch}"\npath="{dest}"\n'
    )
    out = rtfm.list_sources()
    specs = next(s for s in out["sources"] if s["name"] == "specs")
    assert specs["type"] == "git_repo"
    assert specs["url"] == str(remote)
    assert specs["ref"] == branch
    assert "git_status" in specs
    assert specs["git_status"] in ("up to date", "behind", "ahead", "diverged",
                                   "detached", "dirty")


def test_health_check_reports_schema_version(home):
    out = rtfm.health_check()
    assert out["ok"] is True
    assert out["schema_version"] == rtfm.SCHEMA_VERSION


def test_find_duplicates_groups_by_content(home, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    body = "shared content body\n"
    for n in ("a.md", "b.md", "c.md"):
        (d / n).write_text(body)
    (d / "unique.md").write_text("different body\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source("docs", "dir", d))
    out = rtfm.find_duplicates()
    assert len(out["duplicates"]) == 1  # only the 3-way duplicate
    g = out["duplicates"][0]
    assert g["n_locations"] == 3
    assert {loc["relpath"] for loc in g["locations"]} == {"a.md", "b.md", "c.md"}


def test_find_duplicates_source_scopes_count_and_paths(home, tmp_path):
    """source= scopes both the count and listed paths to within-source duplicates."""
    body = "shared content for dedup test\n"

    # Source A: two files with the same content (within-source duplicate)
    d_a = tmp_path / "srcA"
    d_a.mkdir()
    (d_a / "a1.md").write_text(body)
    (d_a / "a2.md").write_text(body)
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="srcA", type="dir", path=d_a))

    # Source B: one file with the same content (not a within-source duplicate)
    d_b = tmp_path / "srcB"
    d_b.mkdir()
    (d_b / "b1.md").write_text(body)
    rtfm.load_manifest()
    _add_source("srcA", d_a)
    _add_source("srcB", d_b)
    rtfm.reindex_source(conn, rtfm.Source(name="srcB", type="dir", path=d_b))

    # Within source A: one group, count=2, only A paths
    out_a = rtfm.find_duplicates(source="srcA")
    assert len(out_a["duplicates"]) == 1
    g = out_a["duplicates"][0]
    assert g["n_locations"] == 2
    assert all(loc["source"] == "srcA" for loc in g["locations"])
    assert {loc["relpath"] for loc in g["locations"]} == {"a1.md", "a2.md"}

    # Within source B: empty (content appears only once in B)
    out_b = rtfm.find_duplicates(source="srcB")
    assert out_b["duplicates"] == []

    # Global (no source): one group, count=3, all three paths across A and B
    out_all = rtfm.find_duplicates()
    assert len(out_all["duplicates"]) == 1
    g_all = out_all["duplicates"][0]
    assert g_all["n_locations"] == 3
    sources_in_group = {loc["source"] for loc in g_all["locations"]}
    assert sources_in_group == {"srcA", "srcB"}
    assert len(g_all["locations"]) == 3


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


def test_reindex_tool_handles_git_repo(home, tmp_path, git_branch):
    """The reindex() MCP tool includes git_repo sources."""
    remote, seed, branch = make_git_repo(tmp_path, git_branch,
                                         filename="a.md",
                                         content="hello world content\n")
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), branch, dest, timeout=30)
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\n'
        f'url="{remote}"\nref="{branch}"\npath="{dest}"\n'
    )
    out = rtfm.reindex(source="specs")
    assert len(out["reindexed"]) == 1
    assert out["reindexed"][0]["source"] == "specs"
    assert out["reindexed"][0]["files_seen"] >= 1


def test_health_check_reports_git_repo_sources(home, tmp_path):
    """health_check includes git_repo sources."""
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        '[[source]]\nname="specs"\ntype="git_repo"\n'
        'url="https://example.com/repo.git"\nref="feat-x"\n'
    )
    out = rtfm.health_check()
    names = [s["name"] for s in out["sources"]]
    assert "specs" in names
    assert any(s["type"] == "git_repo" for s in out["sources"])
