# tests/test_config.py
from pathlib import Path

import rtfm_server as rt


def test_corpus_home_default(monkeypatch):
    monkeypatch.delenv("RTFM_HOME", raising=False)
    assert rt.corpus_home() == Path.home() / ".rtfm"


def test_corpus_home_env_override(home):
    assert rt.corpus_home() == home


def test_derived_paths(home):
    assert rt.manifest_path() == home / "manifest.toml"
    assert rt.default_source_dir() == home / "default"
    assert rt.index_db_path() == home / "cache" / "index.db"
