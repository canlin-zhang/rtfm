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
