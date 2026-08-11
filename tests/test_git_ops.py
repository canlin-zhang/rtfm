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
    """A missing git binary yields a classified RuntimeError, not a raw FileNotFoundError —
    load_manifest ('Never raises') and every MCP tool must survive an absent git."""
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
    import rtfm_server as rtfm

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
    mode is read-only (ADR 0013), so only the tree's HEAD change triggers it."""
    import rtfm_server as rtfm

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
