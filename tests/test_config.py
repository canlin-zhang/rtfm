# tests/test_config.py
from pathlib import Path

import rtfm_server as rtfm


def test_corpus_home_default(monkeypatch):
    monkeypatch.delenv("RTFM_HOME", raising=False)
    assert rtfm.corpus_home() == Path.home() / ".rtfm"


def test_corpus_home_env_override(home):
    assert rtfm.corpus_home() == home


def test_derived_paths(home):
    assert rtfm.manifest_path() == home / "manifest.toml"
    assert rtfm.default_source_dir() == home / "default"
    assert rtfm.index_db_path() == home / "cache" / "index.db"
