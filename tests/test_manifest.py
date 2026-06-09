# tests/test_manifest.py
import rtfm_server as rtfm


def test_bootstrap_creates_default(home):
    sources, warnings = rtfm.load_manifest()           # no manifest yet -> bootstrap
    assert rtfm.manifest_path().exists()
    assert rtfm.default_source_dir().is_dir()
    assert warnings == []
    assert [s.name for s in sources] == ["default"]
    d = sources[0]
    assert d.type == "dir" and d.mutable is True
    assert d.path == rtfm.default_source_dir()


def test_load_explicit_sources(home):
    default = rtfm.default_source_dir()
    (home / "manifest.toml").write_text(
        f'[[source]]\nname="default"\ntype="dir"\npath="{default}"\nmutable=true\n'
        '[[source]]\nname="vendor"\ntype="dir"\npath="/opt/vendor/doc"\n'
    )
    sources, warnings = rtfm.load_manifest()
    assert [s.name for s in sources] == ["default", "vendor"]
    assert sources[1].mutable is False           # defaults false


def test_duplicate_names_first_wins_with_warning(home):
    (home / "manifest.toml").write_text(
        '[[source]]\nname="pa"\ntype="dir"\npath="/opt/a"\n'
        '[[source]]\nname="pa"\ntype="dir"\npath="/opt/b"\n'
    )
    sources, warnings = rtfm.load_manifest()
    assert [s.name for s in sources] == ["pa"]    # second refused
    assert sources[0].path == rtfm.Path("/opt/a")
    assert any("pa" in w and "/opt/b" in w for w in warnings)


def test_name_derived_from_path_when_omitted(home):
    (home / "manifest.toml").write_text(
        '[[source]]\ntype="dir"\npath="/opt/acme-docs-2025.06"\n'
    )
    sources, warnings = rtfm.load_manifest()
    assert sources[0].name == "acme-docs-2025.06"


def test_malformed_manifest_degrades_loudly(home):
    rtfm.load_manifest()                                   # bootstrap a valid manifest
    rtfm.manifest_path().write_text("this is not valid toml = = [[[\n")
    sources, warnings = rtfm.load_manifest()
    assert sources == []
    assert warnings and "MALFORMED" in warnings[0]
    # the search tool must degrade without raising
    out = rtfm.search(query="anything")
    assert out["results"] == [] and "WARNING" in out


def test_missing_path_source_warns_but_keeps_it_and_others(home, tmp_path):
    """A dir source whose path does not exist is kept (so it stays visible) and warned about;
    other sources still load. Never silently dropped, never crashes the load."""
    good = tmp_path / "good"
    good.mkdir()
    (home / "manifest.toml").write_text(
        f'[[source]]\nname="good"\ntype="dir"\npath="{good}"\n'
        '[[source]]\nname="missing"\ntype="dir"\npath="/no/such/place"\n'
    )
    sources, warnings = rtfm.load_manifest()
    assert {s.name for s in sources} == {"good", "missing"}
    assert any("missing" in w and "/no/such/place" in w for w in warnings)


def test_dir_source_without_path_is_dropped_with_warning(home):
    """A dir source with no `path` at all is unusable, so it is dropped — but loudly."""
    (home / "manifest.toml").write_text('[[source]]\nname="nopath"\ntype="dir"\n')
    sources, warnings = rtfm.load_manifest()
    assert all(s.name != "nopath" for s in sources)
    assert any("nopath" in w for w in warnings)


def test_path_not_a_directory_warns(home, tmp_path):
    """A dir source pointed at a file (not a directory) is warned about."""
    f = tmp_path / "afile.md"
    f.write_text("hi\n")
    (home / "manifest.toml").write_text(
        f'[[source]]\nname="filey"\ntype="dir"\npath="{f}"\n'
    )
    sources, warnings = rtfm.load_manifest()
    assert any("filey" in w for w in warnings)


def test_search_tolerates_invalid_source(home, tmp_path):
    """search degrades gracefully when a configured source is broken: it returns hits from the
    good sources and surfaces the warning, never raising."""
    good = tmp_path / "good"
    good.mkdir()
    (good / "g.md").write_text("the widget protocol defines flits\n")
    (home / "manifest.toml").write_text(
        f'[[source]]\nname="default"\ntype="dir"\npath="{rtfm.default_source_dir()}"\nmutable=true\n'
        f'[[source]]\nname="good"\ntype="dir"\npath="{good}"\n'
        '[[source]]\nname="broken"\ntype="dir"\npath="/no/such/place"\n'
    )
    out = rtfm.search(query="widget protocol")
    assert any("widget protocol" in h["snippet"] for h in out["results"])
    assert "WARNING" in out and any("broken" in w for w in out["WARNING"])
