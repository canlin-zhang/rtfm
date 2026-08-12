import os
import subprocess

import pytest

import rtfm_server as rtfm

# Fresh `git init` repos must get a `main` branch — the tests assume main, but the
# machine default may be `master`. Pass init.defaultBranch=main to every git
# subprocess these tests spawn via git's GIT_CONFIG_* environment mechanism.
os.environ.setdefault("GIT_CONFIG_COUNT", "1")
os.environ.setdefault("GIT_CONFIG_KEY_0", "init.defaultBranch")
os.environ.setdefault("GIT_CONFIG_VALUE_0", "main")


# --- _git helper ---

def test_git_runs_and_returns_completed_process(tmp_path):
    cp = rtfm._git(["init", str(tmp_path)], cwd=tmp_path, timeout=10)
    assert cp.returncode == 0
    assert (tmp_path / ".git").is_dir()


def test_git_non_zero_exit_raises(tmp_path):
    with pytest.raises(RuntimeError, match="exit"):
        rtfm._git(["log"], cwd=tmp_path, timeout=10)  # not a git repo


def test_git_missing_binary_raises_classified_error(tmp_path, monkeypatch):
    """A missing git binary yields a classified RuntimeError from _git, not a raw
    FileNotFoundError. The boundaries where that class must surface (reindex linked,
    reindex managed, load_manifest) are pinned by the *_missing_git tests below."""
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty dir: git is unreachable
    with pytest.raises(RuntimeError, match="git executable not found"):
        rtfm._git(["status"], cwd=tmp_path, timeout=10)


# --- git repo identification ---

def test_repo_root_returns_path_for_git_dir(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    assert rtfm._git_repo_root(tmp_path) == tmp_path


def test_repo_root_returns_none_for_non_repo(tmp_path):
    assert rtfm._git_repo_root(tmp_path) is None


# --- remote URL ---

def test_remote_url_returns_origin_url(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://example.com/repo.git"], capture_output=True)
    assert rtfm._git_remote_url(tmp_path) == "https://example.com/repo.git"


def test_remote_url_no_origin_is_empty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    assert rtfm._git_remote_url(tmp_path) == ""


# --- clean / dirty ---

def test_clean_tree_is_clean(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    clean, dirty_files = rtfm._git_is_clean(tmp_path)
    assert clean is True
    assert dirty_files == []


def test_modified_file_is_dirty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    f = tmp_path / "a.txt"
    f.write_text("hello")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)
    f.write_text("modified")
    clean, dirty_files = rtfm._git_is_clean(tmp_path)
    assert clean is False
    assert "a.txt" in dirty_files


def test_untracked_file_is_dirty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    (tmp_path / "new.txt").write_text("untracked")
    clean, dirty_files = rtfm._git_is_clean(tmp_path)
    assert clean is False
    assert "new.txt" in dirty_files


# --- commit ---

def test_current_commit_returns_sha(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    sha = rtfm._git_current_commit(tmp_path)
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_commit_date_returns_iso8601(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    date = rtfm._git_commit_date(tmp_path, "HEAD")
    assert "T" in date  # ISO 8601


# --- resolve ref ---

def test_resolve_ref_returns_sha_for_branch(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    sha = rtfm._git_resolve_ref(tmp_path, "main")
    assert len(sha) == 40


def test_resolve_ref_returns_sha_for_head(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True)
    sha = rtfm._git_resolve_ref(tmp_path, "HEAD")
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
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
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
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    # push a new commit to remote
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    rtfm._git_fetch(dest, timeout=30)
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
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    # push v2 to remote
    (seed / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    # fetch then checkout
    rtfm._git_fetch(dest, timeout=30)
    rtfm._git_checkout(dest, "main")
    assert (dest / "a.md").read_text() == "v2\n"


# --- reindex_source for git_repo ---

def test_reindex_git_repo_linked_clean_indexes_files(home, tmp_path):
    """A linked git_repo (path provided) with a clean tree indexes its files."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("the widget protocol defines flits\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert summary["files_seen"] >= 1
    assert summary["newly_extracted"] >= 1

    # Verify source_meta was recorded
    row = conn.execute(
        "SELECT git_commit, git_commit_date FROM source_meta WHERE source='specs'"
    ).fetchone()
    assert row is not None
    assert len(row[0]) == 40  # SHA
    assert "T" in row[1]       # ISO 8601 date

    # Verify content is searchable
    hits = rtfm.search_index(conn, "widget protocol")
    assert any("widget protocol" in h["snippet"] for h in hits)


def test_reindex_git_repo_dirty_refuses(home, tmp_path):
    """A dirty git_repo refuses to index and returns a classified error."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("clean content\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    # Dirty the tree
    (dest / "guide.md").write_text("locally modified content\n")

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "error" in summary
    assert "dirty" in summary.get("error", "").lower()
    assert "guide.md" in summary.get("error", "")


def test_reindex_git_repo_linked_ahead_indexes_without_mutating(home, tmp_path):
    """A linked git_repo with unpushed local commits is indexed as-is — linked mode
    is read-only (ADR 0013), so rtfm neither fetches nor resets: the local commit,
    the working tree, and the clone's remote-tracking refs all survive."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("published v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    # Remote moves on AND the linked clone has its own unpushed commit
    (seed / "guide.md").write_text("published v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    (dest / "local.md").write_text("unpushed local work\n")
    subprocess.run(["git", "-C", str(dest), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-m", "local work"], capture_output=True)
    local_sha = rtfm._git_current_commit(dest)
    origin_before = rtfm._git(["rev-parse", "origin/main"], cwd=dest,
                              timeout=10).stdout.strip()

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary
    assert summary["files_seen"] >= 1
    assert summary["newly_extracted"] >= 1

    # rtfm never mutates the linked repo: commit, working tree, and remote refs survive
    assert rtfm._git_current_commit(dest) == local_sha
    assert (dest / "local.md").exists()
    origin_after = rtfm._git(["rev-parse", "origin/main"], cwd=dest,
                             timeout=10).stdout.strip()
    assert origin_after == origin_before  # no fetch happened


def test_reindex_git_repo_managed_clones_and_indexes(home, tmp_path):
    """A managed git_repo (no path) clones, fetches, and indexes."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "ref.md").write_text("alpha bravo charlie content\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert summary["files_seen"] >= 1
    assert summary["newly_extracted"] >= 1

    # Verify clone was created at the managed path
    managed = rtfm._managed_repo_path("specs")
    assert managed.is_dir()
    assert (managed / ".git").is_dir()

    # Verify content is searchable
    hits = rtfm.search_index(conn, "alpha bravo")
    assert any("alpha bravo" in h["snippet"] for h in hits)


def test_read_document_text_managed_git_repo_resolves_clone(home, tmp_path):
    """read_document_text resolves a path-less git_repo source to its managed clone."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "ref.md").write_text("line one\nline two\nline three\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    rtfm.reindex_source(conn, src)

    text = rtfm.read_document_text(src, "ref.md", start=2, end=3)
    assert "line two" in text and "line three" in text
    assert "line one" not in text


def test_read_before_reindex_managed_repo_guides_recovery(home, tmp_path):
    """Reading a never-cloned managed git_repo points the user at reindex."""
    src = rtfm.Source(name="specs", type="git_repo", url="https://example.com/r.git")
    text = rtfm.read_document_text(src, "ref.md")
    assert "no clone yet" in text
    assert "reindex('specs')" in text


def test_search_then_read_managed_git_repo(home, tmp_path):
    """The search→read flow works for a managed git_repo (path-less) source."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "ref.md").write_text("alpha bravo charlie\nline two\nline three\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\n'
    )

    # search auto-reindexes the managed clone and returns hit locations with relpaths
    out = rtfm.search(query="alpha bravo")
    assert out["results"]
    hit = out["results"][0]
    assert any(loc["source"] == "specs" and loc["relpath"] == "ref.md"
               for loc in hit["locations"])

    # read the hit's file through the MCP tool — must resolve the managed clone
    text = rtfm.read(source="specs", relpath="ref.md", start=1, end=2)
    assert "alpha bravo charlie" in text
    assert "line three" not in text


# --- search auto-reindex for git_repo ---

def test_search_auto_reindexes_stale_git_repo(home, tmp_path):
    """Search on a stale managed git_repo auto-reindexes (fetch + reset — rtfm owns
    the clone) and returns fresh results."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("the widget protocol defines flits v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    rtfm.load_manifest()  # bootstrap default
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\n'
    )

    # First search clones and indexes v1
    out = rtfm.search(query="widget protocol")
    assert any("v1" in h["snippet"] for h in out["results"])

    # Push v2 to remote
    (seed / "guide.md").write_text("the widget protocol defines flits v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    # Search again — fetches, resets, and reindexes
    out = rtfm.search(query="widget protocol")
    assert any("v2" in h["snippet"] for h in out["results"])


def test_search_auto_reindexes_linked_after_user_refresh(home, tmp_path):
    """A linked git_repo auto-reindexes when the USER moves their checkout — linked
    mode is read-only (ADR 0013), so only the tree's HEAD change or a dirty tree
    triggers it."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("the widget protocol defines flits v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    rtfm.load_manifest()  # bootstrap default
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\npath="{dest}"\n'
    )

    # First search indexes v1
    out = rtfm.search(query="widget protocol")
    assert any("v1" in h["snippet"] for h in out["results"])

    # Push v2; the USER refreshes their own clone (rtfm never fetches for them)
    (seed / "guide.md").write_text("the widget protocol defines flits v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "fetch", "origin"], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-B", "main", "origin/main"],
                    capture_output=True)

    # Search again — the moved HEAD triggers the auto-reindex
    out = rtfm.search(query="widget protocol")
    assert any("v2" in h["snippet"] for h in out["results"])


def test_search_warns_when_git_repo_fetch_fails(home, tmp_path, monkeypatch):
    """When git fetch fails on a managed clone, search warns and serves stale content."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("widget v1 content\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    # Clone into the managed location so the source is 'managed' (rtfm owns it)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    rtfm.reindex_source(conn, src)

    # Push v2
    (seed / "guide.md").write_text("widget v2 content\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    # Break fetch
    def broken_fetch(*a, **k):
        raise RuntimeError("simulated network failure")
    monkeypatch.setattr(rtfm, "_git_fetch", broken_fetch)

    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\n'
    )
    out = rtfm.search(query="widget")
    # Should serve v1 (stale) with a warning
    assert any("v1" in h["snippet"] for h in out["results"])
    assert "WARNING" in out
    assert any("specs" in w and ("fetch" in w.lower() or "auto-reindex" in w.lower())
               for w in out["WARNING"])


# --- round-1 review batch: classified errors and boundary states ---

def _no_git(monkeypatch, tmp_path):
    """Make git unreachable for the duration of a test."""
    emptybin = tmp_path / "emptybin"
    emptybin.mkdir()
    monkeypatch.setenv("PATH", str(emptybin))


def test_reindex_linked_missing_git_is_git_missing(home, tmp_path, monkeypatch):
    """A git-less machine with a linked source gets GIT_MISSING at reindex, never
    the misleading NOT_GIT_REPO (the classification chain's linked boundary)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    _no_git(monkeypatch, tmp_path)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "GIT_MISSING" in summary["error"]
    assert "NOT_GIT_REPO" not in summary["error"]


def test_reindex_managed_missing_git_is_git_missing(home, tmp_path, monkeypatch):
    """A git-less machine with an existing managed clone gets GIT_MISSING at
    reindex (the classification chain's managed boundary)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    _no_git(monkeypatch, tmp_path)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "GIT_MISSING" in summary["error"]


def test_managed_clone_failed_classified(home, tmp_path):
    """A bad managed URL yields ERROR:CLONE_FAILED with recovery steps."""
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo",
                      url=str(tmp_path / "no-such-remote.git"), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "CLONE_FAILED" in summary["error"]
    assert "Recover" in summary["error"]


def test_managed_checkout_failed_classified(home, tmp_path):
    """A ref that doesn't exist on the remote yields ERROR:CHECKOUT_FAILED with
    actionable advice (a dirty tree is classified DIRTY_TREE before checkout, so
    checkout advice is never issued for it)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="no-such-ref")
    summary = rtfm.reindex_source(conn, src)
    assert "CHECKOUT_FAILED" in summary["error"]
    assert "Recover" in summary["error"]


def test_managed_no_remote_classified(home, tmp_path):
    """A managed clone whose origin was removed, with no declared ref: NO_REMOTE."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    subprocess.run(["git", "-C", str(dest), "remote", "remove", "origin"],
                   capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote))  # ref omitted
    summary = rtfm.reindex_source(conn, src)
    assert "NO_REMOTE" in summary["error"]


def test_managed_remote_mismatch_classified(home, tmp_path):
    """A managed clone whose origin points elsewhere: REMOTE_MISMATCH."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo",
                      url=str(tmp_path / "other.git"), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "REMOTE_MISMATCH" in summary["error"]


def test_managed_fake_git_dir_is_not_git_repo(home, tmp_path):
    """A managed path with a fake .git dir (no repo) is NOT_GIT_REPO, not a
    remote failure."""
    dest = rtfm._managed_repo_path("specs")
    (dest / ".git").mkdir(parents=True)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo",
                      url=str(tmp_path / "remote.git"), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "NOT_GIT_REPO" in summary["error"]


def test_search_warns_on_dirty_linked(home, tmp_path):
    """A linked clone with uncommitted edits: search auto-reindex attempts the
    reindex, the DIRTY_TREE refusal warns loudly, and old content is served —
    never silently."""
    import subprocess as sp

    remote = tmp_path / "remote.git"
    sp.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    sp.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "guide.md").write_text("the widget protocol defines flits v1\n")
    sp.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    sp.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    sp.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\npath="{dest}"\n'
    )
    out = rtfm.search(query="widget protocol")
    assert any("v1" in h["snippet"] for h in out["results"])

    # Uncommitted edit — the tree is now dirty
    (dest / "guide.md").write_text("the widget protocol defines flits v2-uncommitted\n")
    out = rtfm.search(query="widget protocol")
    assert any("v1" in h["snippet"] for h in out["results"])  # old content served
    assert "WARNING" in out
    assert any("DIRTY_TREE" in w and "guide.md" in w for w in out["WARNING"])


def test_managed_ref_less_full_flow(home, tmp_path):
    """A managed source with no ref resolves the remote's default branch and works
    end to end."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("alpha bravo charlie\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote))  # no ref
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary
    assert summary["files_seen"] >= 1
    hits = rtfm.search_index(conn, "alpha bravo")
    assert any("alpha bravo" in h["snippet"] for h in hits)


def test_clone_vanished_is_recreated(home, tmp_path):
    """A deleted managed clone (user ran rm -rf ~/.rtfm/repos/<name>) is recreated
    on the next search — the real-world recovery the stale-delta guards."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("alpha bravo charlie\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\n')
    out = rtfm.search(query="alpha bravo")
    assert any("alpha bravo" in h["snippet"] for h in out["results"])

    import shutil
    shutil.rmtree(rtfm._managed_repo_path("specs"))
    out = rtfm.search(query="alpha bravo")
    assert any("alpha bravo" in h["snippet"] for h in out["results"])  # recreated


def test_linked_empty_repo_is_emptied(home, tmp_path):
    """A linked clone with no commits yet (unborn HEAD) is a classified
    ERROR:EMPTY_REPO, never an uncaught crash."""
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", str(dest)], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin",
                    "https://example.com/repo.git"], capture_output=True)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://example.com/repo.git", ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "EMPTY_REPO" in summary["error"]
    assert "no commits" in summary["error"]


@pytest.mark.parametrize("sha_form", ["full", "short"])
def test_managed_sha_ref_first_run(home, tmp_path, sha_form):
    """A managed source pinned to a SHA on a fresh home: git rejects
    `clone --branch <sha>`, so the clone must fall back to the default branch and
    check the SHA out detached — the pin works from the first reindex, in both
    full and short (7-char) SHA forms."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("pinned content\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    sha = rtfm._git_current_commit(seed)
    ref = sha if sha_form == "full" else sha[:7]

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref=ref)
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary
    assert summary["commit"] == sha  # the pinned commit was indexed


def test_tag_only_commit_ref_checkout(home, tmp_path):
    """A managed source re-pointed at a tag whose commit is unreachable from any
    branch: plain fetch cannot bring the tag, the explicit-refspec fallback must."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    assert "error" not in rtfm.reindex_source(conn, src)

    # A commit reachable only through a tag (pushed via a temp branch, then the
    # branch deleted) — plain fetch will not materialize the tag.
    (seed / "a.md").write_text("tagged v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "tagged v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "branch", "tmp"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "tmp"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", ":tmp"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "tag", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "v2"], capture_output=True)

    src2 = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="v2")
    summary = rtfm.reindex_source(conn, src2)
    assert "error" not in summary
    assert summary["files_seen"] >= 1


def test_deleted_linked_clone_is_path_missing(home, tmp_path):
    """A linked clone deleted on disk is PATH_MISSING ('does not exist') with the
    right recovery advice, never the misleading GIT_MISSING."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    import shutil
    shutil.rmtree(dest)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "PATH_MISSING" in summary["error"]
    assert "does not exist" in summary["error"]
    assert "GIT_MISSING" not in summary["error"]


def test_hex_named_branch_not_detached(home, tmp_path):
    """A branch named like a SHA is a normal branch to git — status must not
    report it as a detached pin."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    subprocess.run(["git", "-C", str(dest), "branch", "-m", "main", "deadbeef"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(dest), "push", "origin", "deadbeef"],
                   capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="deadbeef")
    rtfm.reindex_source(conn, src)
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\n'
        f'ref="deadbeef"\npath="{dest}"\n'
    )
    out = rtfm.list_sources()
    specs = next(s for s in out["sources"] if s["name"] == "specs")
    assert specs["git_status"] == "up to date"  # a branch, not a pin


def test_remote_mismatch_normalized(home, tmp_path):
    """https://host/org/repo.git and https://host/org/repo are the same remote —
    the mismatch check must normalize before comparing."""
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", str(dest)], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin",
                    "https://example.com/org/repo.git"], capture_output=True)
    (dest / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(dest), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-m", "v1"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://example.com/org/repo", ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary


# --- round-2 review batch: crash paths, pin changes, fetch counts, docs ---

def test_managed_ref_less_missing_git_is_git_missing(home, tmp_path, monkeypatch):
    """A git-less machine with a ref-less managed source: the classification chain
    must not crash — the clone-failure path called _is_sha(ref) with ref=None,
    which TypeError'd instead of producing GIT_MISSING."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    _no_git(monkeypatch, tmp_path)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote))  # no ref
    summary = rtfm.reindex_source(conn, src)
    assert "GIT_MISSING" in summary["error"]


def test_managed_ref_less_bad_url_clone_failed(home, tmp_path):
    """A ref-less managed source with a bad URL: CLONE_FAILED, never a TypeError."""
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo",
                      url=str(tmp_path / "no-such-remote.git"))  # no ref
    summary = rtfm.reindex_source(conn, src)
    assert "CLONE_FAILED" in summary["error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_reindex_linked_corrupt_index_is_git_failed(home, tmp_path):
    """A corrupt .git/index (crash, power loss) surfaces at the dirty check: the
    GIT_FAILED class must fire, never a raw RuntimeError or NOT_GIT_REPO."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    (dest / ".git" / "index").write_bytes(b"garbage")  # corrupt the index

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "GIT_FAILED" in summary["error"]
    assert "NOT_GIT_REPO" not in summary["error"]


def test_managed_dirty_tree_classified(home, tmp_path):
    """A dirty managed clone is classified DIRTY_TREE with the right advice, never
    the dead-end CHECKOUT_FAILED (the dirty check runs before checkout)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    assert "error" not in rtfm.reindex_source(conn, src)

    (dest / "a.md").write_text("local edit\n")  # dirty the managed clone
    summary = rtfm.reindex_source(conn, src)
    assert "DIRTY_TREE" in summary["error"]
    assert "CHECKOUT_FAILED" not in summary["error"]


def test_remote_mismatch_ssh_form_and_case(home, tmp_path):
    """ssh-form, https-form, and mixed-case URLs for the same repo all compare
    equal — the normalization claim's headline cases."""
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", str(dest)], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin",
                    "git@example.com:org/repo.git"], capture_output=True)
    (dest / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(dest), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-m", "v1"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://Example.COM/Org/Repo", ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary


def test_remote_mismatch_port_and_trailing_slash(home, tmp_path):
    """Ported ssh URLs and trailing slashes are the same remote; a genuinely
    different repo still mismatches."""
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", str(dest)], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin",
                    "ssh://git@example.com:22/org/repo.git"], capture_output=True)
    (dest / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(dest), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-m", "v1"], capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://example.com/org/repo/", ref="main")
    assert "error" not in rtfm.reindex_source(conn, src)

    bad = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://example.com/other/repo", ref="main")
    summary = rtfm.reindex_source(conn, bad)
    assert "REMOTE_MISMATCH" in summary["error"]


def test_fetch_counts(monkeypatch, home, tmp_path):
    """The fetch-count contract: one fetch for a managed refresh, zero for linked —
    the double-fetch regression of round 1 must never come back."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    calls = []
    real_fetch = rtfm._git_fetch

    def counting_fetch(path, timeout=None):
        calls.append(str(path))
        return real_fetch(path, timeout=timeout)

    monkeypatch.setattr(rtfm, "_git_fetch", counting_fetch)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    rtfm.reindex_source(conn, src)
    assert len(calls) == 1  # the managed refresh: exactly one fetch

    linked = tmp_path / "linked"
    rtfm._git_clone(str(remote), "main", linked, timeout=30)
    src2 = rtfm.Source(name="specs2", type="git_repo", path=linked,
                       url=str(remote), ref="main")
    rtfm.reindex_source(conn, src2)
    assert len(calls) == 1  # linked reindex: zero fetches


def test_managed_hex_branch_first_run(home, tmp_path):
    """A managed first run against a hex-named branch: clone --branch deadbeef
    works (git treats it as a branch), status is 'up to date', not 'detached'."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-m", "main", "deadbeef"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "deadbeef"],
                   capture_output=True)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="deadbeef")
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="deadbeef"\n'
    )
    out = rtfm.list_sources()
    specs = next(s for s in out["sources"] if s["name"] == "specs")
    assert specs["git_status"] == "up to date"


def test_managed_fetch_failed_classified(home, tmp_path):
    """A fetch that fails on an existing managed clone is the documented
    FETCH_FAILED class with recovery advice."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    import shutil
    shutil.rmtree(remote)  # the origin vanishes — fetch fails, URL still matches

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "FETCH_FAILED" in summary["error"]
    assert "Recover" in summary["error"]


def test_list_sources_error_status(home, tmp_path):
    """A git call that fails inside list_sources degrades to an 'error: ...'
    status string — the ninth documented value, and the 'one bad source never
    breaks the listing' contract."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="main")
    rtfm.reindex_source(conn, src)
    (dest / ".git" / "index").write_bytes(b"garbage")  # corrupt → status fails

    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="main"\npath="{dest}"\n'
    )
    out = rtfm.list_sources()
    specs = next(s for s in out["sources"] if s["name"] == "specs")
    assert specs["git_status"].startswith("error:")


def test_health_check_git_probe(home, tmp_path, monkeypatch):
    """health_check reports git presence and flips ok=False when git_repo sources
    exist on a git-less machine."""
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        '[[source]]\nname="specs"\ntype="git_repo"\n'
        'url="https://example.com/repo.git"\nref="main"\n'
    )
    _no_git(monkeypatch, tmp_path)
    out = rtfm.health_check()
    assert out["git"] is False
    assert out["ok"] is False
    assert any("git" in issue.lower() for issue in out["issues"])


def test_linked_unparseable_config_is_git_failed(home, tmp_path):
    """A genuinely unparseable .git/config fails the repo probe: classified
    GIT_FAILED, never a raw exception or a wrong class. (A merely odd config —
    `[core]\nbroken` is valid syntax — makes git report the remote as missing and
    is covered by test_linked_no_origin_proceeds.)"""
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", str(dest)], capture_output=True)
    (dest / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(dest), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-m", "v1"], capture_output=True)
    (dest / ".git" / "config").write_text("[core\nbroken=")  # unparseable
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://example.com/repo.git", ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "GIT_FAILED" in summary["error"]


def test_linked_no_origin_proceeds(home, tmp_path):
    """A linked clone without an origin remote is indexed as-is (the mismatch
    check has nothing to compare) — the ''-design half of the contract."""
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", str(dest)], capture_output=True)
    (dest / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(dest), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-m", "v1"], capture_output=True)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url="https://example.com/repo.git", ref="main")
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary


def test_managed_pin_change_detected(home, tmp_path):
    """Changing the manifest pin from one SHA to another is detected — this test
    pins the explicit reindex path; the search-triggered AUTO path (the round-2
    fix) is pinned by test_managed_pin_change_detected_on_search."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("pin v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    sha1 = rtfm._git_current_commit(seed)
    (seed / "a.md").write_text("pin v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    sha2 = rtfm._git_current_commit(seed)

    conn = rtfm.get_index_db()
    src1 = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref=sha1)
    assert "error" not in rtfm.reindex_source(conn, src1)

    src2 = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref=sha2)
    summary = rtfm.reindex_source(conn, src2)  # explicit reindex works either way
    assert "error" not in summary
    assert summary["commit"] == sha2


def test_reindex_purges_manifest_absent_sources(home, tmp_path):
    """Rebuild semantics: a source dropped from the manifest (or dropped at load,
    e.g. url-less git_repo) must not keep serving its leftover index rows."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("orphan content keyword\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="gone", type="dir", path=d))
    assert rtfm.search_index(conn, "orphan content")

    rtfm.load_manifest()  # bootstrap default (no 'gone' source)
    rtfm.reindex()
    assert not rtfm.search_index(conn, "orphan content")
    assert conn.execute(
        "SELECT COUNT(*) FROM locations WHERE source='gone'").fetchone()[0] == 0


# --- round-3 review batch: memo semantics, purge guard, final gaps ---

def test_staleness_memo_bounds_checks_and_invalidates(home, tmp_path, monkeypatch):
    """The verdict memo: two _stale_delta calls within the TTL run one check (one
    fetch for managed); an explicit reindex invalidates the entry."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = rtfm._managed_repo_path("specs")
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", url=str(remote), ref="main")
    rtfm.reindex_source(conn, src)

    calls = []
    real_fetch = rtfm._git_fetch

    def counting_fetch(path, timeout=None):
        calls.append(1)
        return real_fetch(path, timeout=timeout)

    monkeypatch.setattr(rtfm, "_git_fetch", counting_fetch)
    changed, stale, cached = rtfm._stale_delta(conn, src)
    assert (changed, stale, cached) == (0, False, False) and len(calls) == 1
    changed, stale, cached = rtfm._stale_delta(conn, src)
    assert cached is True and len(calls) == 1  # memoized — no second fetch
    rtfm.reindex_source(conn, src)             # invalidates the entry (its own fetch +1)
    changed, stale, cached = rtfm._stale_delta(conn, src)
    assert cached is False and len(calls) == 3  # fresh check again (1 + 1 + 1)


def test_search_does_not_reattempt_broken_source_within_ttl(home, tmp_path, monkeypatch):
    """A broken source is attempted on the first query of each memo window only —
    the reindex attempt and its failure warning must not repeat on every query."""
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="specs"\ntype="git_repo"\n'
        f'url="{tmp_path / "no-such-remote.git"}"\nref="main"\n'
    )
    attempts = []
    real_reindex = rtfm.reindex_source

    def counting_reindex(conn, src):
        attempts.append(src.name)
        return real_reindex(conn, src)

    monkeypatch.setattr(rtfm, "reindex_source", counting_reindex)

    out1 = rtfm.search(query="widget")
    assert len(attempts) == 1
    assert any("CLONE_FAILED" in w for w in out1["WARNING"])
    out2 = rtfm.search(query="widget")   # within the TTL — cached verdict
    assert len(attempts) == 1            # no re-attempt
    assert "WARNING" not in out2         # no repeated warning


def test_managed_pin_change_detected_on_search(home, tmp_path):
    """The AUTO path for a pin change: manifest edited to a new pin, the next
    search (immediately, within any memo window) reindexes to the new pin — the
    memo is keyed by (name, ref), so a manifest edit invalidates instantly."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("pin v1 keyword\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    sha1 = rtfm._git_current_commit(seed)
    (seed / "a.md").write_text("pin v2 keyword\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    sha2 = rtfm._git_current_commit(seed)

    rtfm.load_manifest()
    mp = rtfm.manifest_path()

    def write_manifest(ref):
        mp.write_text(
            f'[[source]]\nname="specs"\ntype="git_repo"\nurl="{remote}"\nref="{ref}"\n'
        )

    write_manifest(sha1)
    out = rtfm.search(query="keyword")
    assert any("pin v1" in h["snippet"] for h in out["results"])
    write_manifest(sha2)
    out = rtfm.search(query="keyword")   # immediately — no 30s wait
    assert any("pin v2" in h["snippet"] for h in out["results"])


def test_search_dedupes_warnings_per_clone(home, tmp_path):
    """Two sources on one dirty clone: exactly one AUTO-REINDEX FAILED warning."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("shared keyword body\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="s1", type="git_repo", path=dest, url=str(remote), ref="main")
    src2 = rtfm.Source(name="s2", type="git_repo", path=dest, url=str(remote), ref="main")
    rtfm.reindex_source(conn, src)
    rtfm.reindex_source(conn, src2)

    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="s1"\ntype="git_repo"\nurl="{remote}"\nref="main"\npath="{dest}"\n'
        f'[[source]]\nname="s2"\ntype="git_repo"\nurl="{remote}"\nref="main"\npath="{dest}"\n'
    )
    (dest / "a.md").write_text("uncommitted edit\n")  # dirty the shared clone

    out = rtfm.search(query="shared keyword")
    dirty_warns = [w for w in out["WARNING"] if "DIRTY_TREE" in w]
    assert len(dirty_warns) == 1  # one warning per clone, not per source


def test_reindex_linked_typo_ref_warns(home, tmp_path):
    """A linked ref that doesn't resolve is a summary warning (never an error),
    with advice covering both a typo and a ref not yet fetched into the clone."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)

    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo", path=dest,
                      url=str(remote), ref="matser")
    summary = rtfm.reindex_source(conn, src)
    assert "error" not in summary
    assert "does not resolve" in summary["warning"]
    assert "fetch --tags" in summary["warning"]


def test_git_clone_hex_ref_bad_url_single_attempt(home, tmp_path, monkeypatch):
    """A hex-shaped ref with a bad URL: exactly one clone attempt — the fallback
    retry only fires on git's 'Remote branch not found' signature."""
    calls = []
    real_git = rtfm._git

    def counting_git(args, cwd, timeout=None, allow_exit_codes=()):
        if args[:1] == ["clone"]:
            calls.append(args)
        return real_git(args, cwd=cwd, timeout=timeout, allow_exit_codes=allow_exit_codes)

    monkeypatch.setattr(rtfm, "_git", counting_git)
    conn = rtfm.get_index_db()
    src = rtfm.Source(name="specs", type="git_repo",
                      url=str(tmp_path / "no-such-remote.git"), ref="a" * 40)
    summary = rtfm.reindex_source(conn, src)
    assert "CLONE_FAILED" in summary["error"]
    assert len(calls) == 1


def test_reindex_malformed_manifest_does_not_purge(home, tmp_path):
    """A malformed manifest must not wipe the index — the purge only runs on a
    parsed manifest (SQLite's `NOT IN ()` is true for every row; round 3's wipe)."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("survivor keyword\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d))
    mp = rtfm.manifest_path()
    mp.write_text("[[source\nbroken")  # unparseable TOML
    resp = rtfm.reindex()
    assert "purged_sources" not in resp
    assert rtfm.search_index(conn, "survivor keyword")  # rows survived


def test_search_sources_searched_respects_filter(home, tmp_path):
    """sources_searched reflects the sources actually iterated, not the whole
    manifest — a filtered search must not claim it searched everything."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("filtered keyword body\n")
    conn = rtfm.get_index_db()
    rtfm.reindex_source(conn, rtfm.Source(name="docs", type="dir", path=d))
    rtfm.load_manifest()
    mp = rtfm.manifest_path()
    mp.write_text(
        f'[[source]]\nname="docs"\ntype="dir"\npath="{d}"\n'
        '[[source]]\nname="typoed"\ntype="Git_Repo"\nurl="https://example.com/r.git"\n'
    )
    out = rtfm.search(query="filtered keyword", source="docs")
    assert out["sources_searched"] == ["docs"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_reindex_unreadable_clone_is_git_failed(home, tmp_path):
    """A chmod-000 linked clone: the OSError wrap classifies GIT_FAILED instead
    of leaking a raw PermissionError past the RuntimeError-only guards."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True)
    (seed / "a.md").write_text("v1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "v1"], capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], capture_output=True)
    dest = tmp_path / "dest"
    rtfm._git_clone(str(remote), "main", dest, timeout=30)
    dest.chmod(0o000)
    try:
        conn = rtfm.get_index_db()
        src = rtfm.Source(name="specs", type="git_repo", path=dest,
                          url=str(remote), ref="main")
        summary = rtfm.reindex_source(conn, src)
        assert "GIT_FAILED" in summary["error"]
    finally:
        dest.chmod(0o755)  # let pytest clean up the tmp dir
