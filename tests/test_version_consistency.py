# tests/test_version_consistency.py
"""Contract tests for scripts/check_version_consistency.py and scripts/bump_version.py.

The check script's whole contract is "exit 0 = consistent, 1 = drift"; these
tests assert the drift paths really return 1 (a regression here would silently
re-enable the 0.5.1 drift bug class with CI still green). Fixture trees stand in
for the repo via monkeypatched module ROOT.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


check = _load("check_version_consistency", ROOT / "scripts/check_version_consistency.py")
bump = _load("bump_version", ROOT / "scripts/bump_version.py")
launch = _load("launch", ROOT / "bin" / "launch")

PEP723 = """# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.28.1,<2",
#   "pymupdf",
#   "pypdf",
# ]
# ///
"""


def make_tree(tmp_path, version="0.5.1", plugin_version=None, lock_version=None,
              script_deps=PEP723, lock_names=("rtfm",)):
    """A minimal repo skeleton: the four files the guard reads, all consistent."""
    plugin_version = version if plugin_version is None else plugin_version
    lock_version = version if lock_version is None else lock_version
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "rtfm"\nversion = "{version}"\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n'
    )
    lock = "".join(
        f'[[package]]\nname = "{name}"\nversion = "{lock_version if name == "rtfm" else "0.1.0"}"\n'
        'source = { virtual = "." }\n'
        for name in lock_names
    )
    (tmp_path / "uv.lock").write_text(lock)
    (tmp_path / ".claude-plugin").mkdir(parents=True)
    # Pretty-printed like the real file: "version" is the last key, no trailing
    # comma — still valid JSON, and the comma-preservation path is exercised.
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "rtfm", "version": plugin_version}, indent=2) + "\n"
    )
    (tmp_path / "rtfm_server.py").write_text(script_deps)


# --- check script -------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: (t / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "rtfm", "version": "0.5.0"})
        ),
        lambda t: (t / "uv.lock").write_text(
            (t / "uv.lock").read_text().replace('version = "0.5.1"', 'version = "0.5.0"')
        ),
        lambda t: (t / "pyproject.toml").write_text(
            (t / "pyproject.toml").read_text().replace('version = "0.5.1"', 'version = "0.5.0"')
        ),
    ],
)
def test_check_version_drift_in_any_file_exits_1(tmp_path, monkeypatch, mutate):
    make_tree(tmp_path)
    mutate(tmp_path)
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_consistent_tree_exits_0(tmp_path, monkeypatch):
    make_tree(tmp_path)
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 0


def test_check_dep_specifier_drift_names_both_sides(tmp_path, monkeypatch, capsys):
    """Unpinned mcp in the PEP-723 block vs pinned in pyproject (the 0.5.1 class):
    exit 1, and the message shows the exact differing specifier from each file."""
    make_tree(tmp_path, script_deps=PEP723.replace('"mcp[cli]>=1.28.1,<2"', '"mcp[cli]"'))
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1
    out = capsys.readouterr()
    combined = out.out + out.err  # differing specifiers print to stdout, ERROR to stderr
    assert "mcp[cli]" in combined and "mcp[cli]>=1.28.1,<2" in combined


def test_check_missing_rtfm_lock_entry_exits_1(tmp_path, monkeypatch):
    make_tree(tmp_path, lock_names=("other",))
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_missing_file_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """A missing file is a clean SystemExit with a message, never a FileNotFoundError
    traceback — pytest.raises(SystemExit) would fail the test on the latter."""
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").unlink()
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="plugin.json not found"):
        check.main()


def test_check_malformed_pyproject_is_clean_error_not_traceback(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text("this is not [ toml")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="unparseable"):
        check.main()


def test_checker_and_launcher_parsers_agree_on_real_server():
    """bin/launch tolerates `# ?` comment prefixes; the checker requires `# `. If
    formatting ever shifts so the two parsers diverge, this fails — the guard and
    the launcher must always read the same dependency list from the real file."""
    server = ROOT / "rtfm_server.py"
    assert set(check._read_pep723_deps(server)) == set(launch.parse_pep723_deps(server))


# --- bump script --------------------------------------------------------------


def make_runner(lock_ok=True, self_check_ok=0):
    calls = []

    def fake(args, **kwargs):
        calls.append((args, kwargs.get("cwd")))
        if args and args[0] == "uv":
            if not lock_ok:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, self_check_ok)  # the self-check script

    return fake, calls


def run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=None):
    monkeypatch.setattr(bump, "ROOT", tmp_path)
    monkeypatch.setattr(bump.subprocess, "run", runner if runner is not None else make_runner()[0])
    monkeypatch.setattr(sys, "argv", ["bump_version.py", new_version])
    return bump.main()


def test_bump_updates_all_declarations_and_self_checks(tmp_path, monkeypatch):
    make_tree(tmp_path)
    fake, calls = make_runner()
    assert run_bump(tmp_path, monkeypatch, runner=fake) == 0

    assert 'version = "0.5.2"' in (tmp_path / "pyproject.toml").read_text()
    # plugin.json's "version" is the LAST key in the fixture (no trailing comma);
    # the result must still be valid JSON — the comma-injection regression test.
    plugin = json.loads((tmp_path / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == "0.5.2"

    assert ["uv", "lock"] in [a for a, _ in calls]
    assert all(cwd == tmp_path for _, cwd in calls)


def test_bump_refuses_on_pre_existing_drift(tmp_path, monkeypatch):
    make_tree(tmp_path, plugin_version="0.5.0")
    with pytest.raises(SystemExit, match="disagree"):
        run_bump(tmp_path, monkeypatch)


def test_bump_rejects_non_pep440_version(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="usage"):
        run_bump(tmp_path, monkeypatch, new_version="not-a-version")


def test_bump_uv_lock_failure_is_clean_error_with_revert_instructions(
    tmp_path, monkeypatch
):
    """`uv lock` failing (the 0.5.1 resolution-failure class) must exit with a
    message that owns the half-edited tree — pytest.raises(SystemExit) would
    fail on a raw CalledProcessError traceback instead."""
    make_tree(tmp_path)
    fake, _ = make_runner(lock_ok=False)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=fake)
    msg = str(exc.value.code)
    assert "already edited" in msg and "git checkout" in msg


def test_bump_self_check_failure_aborts(tmp_path, monkeypatch):
    make_tree(tmp_path)
    fake, _ = make_runner(self_check_ok=1)
    with pytest.raises(SystemExit, match="post-bump consistency check failed"):
        run_bump(tmp_path, monkeypatch, runner=fake)


def test_bump_refuses_ambiguous_version_lines(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        (tmp_path / "pyproject.toml").read_text()
        + '\n[tool.other]\nversion = "0.5.1"\n'
    )
    with pytest.raises(SystemExit, match="expected exactly one"):
        run_bump(tmp_path, monkeypatch)
