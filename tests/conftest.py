import os

import pytest

from rtfm import index

# Fresh `git init` repos must get a `main` branch — the tests assume main, but the
# machine default may be `master`. Pass init.defaultBranch=main to every git
# subprocess spawned with git's GIT_CONFIG_* environment mechanism.
os.environ.setdefault("GIT_CONFIG_COUNT", "1")
os.environ.setdefault("GIT_CONFIG_KEY_0", "init.defaultBranch")
os.environ.setdefault("GIT_CONFIG_VALUE_0", "main")


@pytest.fixture(autouse=True)
def _clear_freshness_cache():
    """The git_repo freshness memo is module-level and TTL'd — a verdict cached by
    one test must not leak into the next (or the same test's later assertions)."""
    index.freshness_cache.clear()
    yield
    index.freshness_cache.clear()


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Ensure every git subprocess has an identity — CI runners lack a global config."""
    for key, val in [
        ("GIT_AUTHOR_NAME", "rtfm-test"),
        ("GIT_AUTHOR_EMAIL", "rtfm@test"),
        ("GIT_COMMITTER_NAME", "rtfm-test"),
        ("GIT_COMMITTER_EMAIL", "rtfm@test"),
    ]:
        monkeypatch.setenv(key, val)


@pytest.fixture(params=["main", "master", "feat-x", "v2.0-rc1"])
def git_branch(request):
    """Branch names to test against — covers common defaults and edge cases."""
    return request.param


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point RTFM_HOME at a tmp dir for the duration of a test."""
    h = tmp_path / "rtfmhome"
    h.mkdir()
    monkeypatch.setenv("RTFM_HOME", str(h))
    return h


@pytest.fixture
def sample_pdf(tmp_path):
    """A 2-page PDF with known text, written via pymupdf."""
    import fitz
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "alpha bravo charlie on page one")
    p2 = doc.new_page()
    p2.insert_text((72, 72), "delta echo foxtrot on page two")
    path = tmp_path / "sample.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def sample_txt(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("\n".join(f"line {i} keyword{i}" for i in range(1, 121)))
    return path


@pytest.fixture
def unopenable():
    """Make a path unopenable for the duration of a test, restoring its mode afterwards —
    pytest's tmp_path teardown cannot remove a directory it has no permission to enter."""
    touched: list = []

    def _make(path, *, directory=False):
        if directory:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.write_text("unreadable")
        touched.append((path, path.stat().st_mode))
        path.chmod(0o000)
        return path

    yield _make
    for path, mode in touched:
        try:
            path.chmod(mode)
        except OSError:
            pass
