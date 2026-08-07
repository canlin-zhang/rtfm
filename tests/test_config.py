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


def test_managed_repo_path(home):
    assert rtfm._managed_repo_path("myspecs") == home / "repos" / "myspecs"


def test_git_timeout_default(monkeypatch):
    monkeypatch.delenv("RTFM_GIT_TIMEOUT", raising=False)
    assert rtfm._git_timeout() == 60


def test_git_timeout_env_override(monkeypatch):
    monkeypatch.setenv("RTFM_GIT_TIMEOUT", "120")
    assert rtfm._git_timeout() == 120


def test_git_timeout_zero_disables(monkeypatch):
    monkeypatch.setenv("RTFM_GIT_TIMEOUT", "0")
    assert rtfm._git_timeout() == 0
