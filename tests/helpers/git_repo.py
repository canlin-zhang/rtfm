"""A local bare remote plus a seeded clone, shared by the git-source tests."""

import subprocess


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
