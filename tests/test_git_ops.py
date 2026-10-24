import subprocess

import pytest

from rtfm import git_ops

# --- run ---

def test_git_run_returns_the_completed_process(tmp_path):
    cp = git_ops.run(["init", str(tmp_path)], cwd=tmp_path, timeout=10)
    assert cp.returncode == 0
    assert (tmp_path / ".git").is_dir()


def test_git_run_raises_on_non_zero_exit(tmp_path):
    with pytest.raises(RuntimeError, match="exit"):
        git_ops.run(["log"], cwd=tmp_path, timeout=10)  # not a git repo


def test_git_missing_binary_raises_classified_error(tmp_path, monkeypatch):
    """A missing git binary yields a classified RuntimeError from _git, not a raw
    FileNotFoundError. The boundaries where that class must surface (reindex linked,
    reindex managed, load_manifest) are pinned by the *_missing_git tests below."""
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty dir: git is unreachable
    with pytest.raises(RuntimeError, match="git executable not found"):
        git_ops.run(["status"], cwd=tmp_path, timeout=10)


# --- git repo identification ---

def test_repo_root_returns_path_for_git_dir(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    assert git_ops.repo_root(tmp_path) == tmp_path


def test_repo_root_returns_none_for_non_repo(tmp_path):
    assert git_ops.repo_root(tmp_path) is None


# --- remote URL ---

def test_remote_url_returns_origin_url(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://example.com/repo.git"], capture_output=True)
    assert git_ops.remote_url(tmp_path) == "https://example.com/repo.git"


def test_remote_url_no_origin_is_empty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    assert git_ops.remote_url(tmp_path) == ""


# --- clean / dirty ---

def test_clean_tree_reports_clean(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    clean, dirty_files = git_ops.is_clean(tmp_path)
    assert clean is True
    assert dirty_files == []


def test_modified_file_reports_dirty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    f = tmp_path / "a.txt"
    f.write_text("hello")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)
    f.write_text("modified")
    clean, dirty_files = git_ops.is_clean(tmp_path)
    assert clean is False
    assert "a.txt" in dirty_files


def test_untracked_file_reports_dirty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    (tmp_path / "new.txt").write_text("untracked")
    clean, dirty_files = git_ops.is_clean(tmp_path)
    assert clean is False
    assert "new.txt" in dirty_files


# --- commit ---

def test_current_commit_returns_sha(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    sha = git_ops.current_commit(tmp_path)
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_commit_date_returns_iso8601(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    date = git_ops.commit_date(tmp_path, "HEAD")
    assert "T" in date  # ISO 8601


# --- resolve ref ---

def test_resolve_ref_returns_sha_for_branch(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    sha = git_ops.resolve_ref(tmp_path, "main")
    assert len(sha) == 40


def test_resolve_ref_returns_sha_for_head(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    sha = git_ops.resolve_ref(tmp_path, "HEAD")
    assert len(sha) == 40


# --- clone ---

def test_clone_creates_repo_at_dest(tmp_path):
    # Create a bare "remote" repo
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    # Seed it with a commit on main
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "f.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    git_ops.clone(str(remote), "main", dest, timeout=30)
    assert (dest / ".git").is_dir()
    assert (dest / "f.md").read_text() == "hello\n"


# --- fetch ---

def test_fetch_updates_remote_refs(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    # initial clone
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    git_ops.clone(str(remote), "main", dest, timeout=30)

    # push a new commit to remote
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    git_ops.fetch(dest, timeout=30)
    # After fetch, origin/main should be at the new commit
    subprocess.run(["git", "-C", str(dest), "checkout", "origin/main"], capture_output=True)
    assert (dest / "a.md").read_text() == "v2\n"


# --- checkout ---

def test_checkout_branch_resets_to_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    git_ops.clone(str(remote), "main", dest, timeout=30)

    # push v2 to remote
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    # fetch then checkout
    git_ops.fetch(dest, timeout=30)
    git_ops.checkout(dest, "main")
    assert (dest / "a.md").read_text() == "v2\n"

