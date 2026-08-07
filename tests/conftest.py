import os
import subprocess

import pytest


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


def make_git_repo(tmp_path, branch, remote_name="remote.git", seed_name="seed",
                  filename="f.md", content="placeholder\n"):
    """Create a bare remote, seed it with one commit on `branch`, push, return
    (remote_path, seed_path, branch)."""
    remote = tmp_path / remote_name
    subprocess.run(
        ["git", "-c", f"init.defaultBranch={branch}", "init", "--bare", str(remote)],
        capture_output=True)
    seed = tmp_path / seed_name
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / filename).write_text(content)
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "seed"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", branch],
                   capture_output=True)
    return remote, seed, branch


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
