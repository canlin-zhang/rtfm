# tests/test_manifest.py
import rtfm_server as rt


def test_bootstrap_creates_default(home):
    sources, warnings = rt.load_manifest()           # no manifest yet -> bootstrap
    assert rt.manifest_path().exists()
    assert rt.default_source_dir().is_dir()
    assert warnings == []
    assert [s.name for s in sources] == ["default"]
    d = sources[0]
    assert d.type == "dir" and d.mutable is True
    assert d.path == rt.default_source_dir()


def test_load_explicit_sources(home):
    (home / "manifest.toml").write_text(
        '[[source]]\nname="default"\ntype="dir"\npath="%s"\nmutable=true\n'
        '[[source]]\nname="vendor"\ntype="dir"\npath="/opt/vendor/doc"\n'
        % rt.default_source_dir()
    )
    sources, warnings = rt.load_manifest()
    assert [s.name for s in sources] == ["default", "vendor"]
    assert sources[1].mutable is False           # defaults false


def test_duplicate_names_first_wins_with_warning(home):
    (home / "manifest.toml").write_text(
        '[[source]]\nname="pa"\ntype="dir"\npath="/opt/a"\n'
        '[[source]]\nname="pa"\ntype="dir"\npath="/opt/b"\n'
    )
    sources, warnings = rt.load_manifest()
    assert [s.name for s in sources] == ["pa"]    # second refused
    assert sources[0].path == rt.Path("/opt/a")
    assert any("pa" in w and "/opt/b" in w for w in warnings)


def test_name_derived_from_path_when_omitted(home):
    (home / "manifest.toml").write_text(
        '[[source]]\ntype="dir"\npath="/opt/acme-docs-2025.06"\n'
    )
    sources, warnings = rt.load_manifest()
    assert sources[0].name == "acme-docs-2025.06"


def test_malformed_manifest_degrades_loudly(home):
    rt.load_manifest()                                   # bootstrap a valid manifest
    rt.manifest_path().write_text("this is not valid toml = = [[[\n")
    sources, warnings = rt.load_manifest()
    assert sources == []
    assert warnings and "MALFORMED" in warnings[0]
    # the search tool must degrade without raising
    out = rt.search(query="anything")
    assert out["results"] == [] and "WARNING" in out
