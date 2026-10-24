# tests/test_config.py
from pathlib import Path

from rtfm import config


def test_corpus_home_defaults_to_home_rtfm(monkeypatch):
    monkeypatch.delenv("RTFM_HOME", raising=False)
    assert config.corpus_home() == Path.home() / ".rtfm"


def test_corpus_home_honours_rtfm_home(home):
    assert config.corpus_home() == home


def test_paths_derive_from_corpus_home(home):
    assert config.manifest_path() == home / "manifest.toml"
    assert config.default_source_dir() == home / "default"
    assert config.index_db_path() == home / "cache" / "index.db"


def test_managed_repo_path_lives_under_repos(home):
    assert config.managed_repo_path("myspecs") == home / "repos" / "myspecs"


def test_git_timeout_defaults_to_60_seconds(monkeypatch):
    monkeypatch.delenv("RTFM_GIT_TIMEOUT", raising=False)
    assert config.git_timeout() == 60


def test_git_timeout_honours_env(monkeypatch):
    monkeypatch.setenv("RTFM_GIT_TIMEOUT", "120")
    assert config.git_timeout() == 120


def test_git_timeout_zero_disables_the_timeout(monkeypatch):
    monkeypatch.setenv("RTFM_GIT_TIMEOUT", "0")
    assert config.git_timeout() == 0
