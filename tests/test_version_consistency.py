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


def make_tree(
    tmp_path,
    version="0.5.1",
    plugin_version=None,
    lock_version=None,
    script_deps=PEP723,
    lock_names=("rtfm",),
):
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
    # Deliberately NOT the real-file shape: here "version" is the last key with no
    # trailing comma, so the `,?` pattern's empty branch is the default exercise.
    # test_bump_preserves_comma_when_version_is_mid_object covers the real shape.
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "rtfm", "version": plugin_version}, indent=2) + "\n"
    )
    (tmp_path / "rtfm_server.py").write_text(script_deps)
    # The real repo carries the check script next to the bump script; the
    # bump's existence guard needs the skeleton to mirror that.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_version_consistency.py").write_text("# stub\n")


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
    # Full-line messages, not substrings: "mcp[cli]" alone would match the pinned
    # specifier too, so each side must be named on its own "only in ..." line.
    assert "only in rtfm_server.py PEP-723 block: mcp[cli]" in combined
    assert "only in pyproject.toml: mcp[cli]>=1.28.1,<2" in combined


def test_check_missing_rtfm_lock_entry_exits_1(tmp_path, monkeypatch):
    make_tree(tmp_path, lock_names=("other",))
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_lock_missing_package_array_is_clean_error(tmp_path, monkeypatch):
    """A uv.lock that parses but has no [[package]] table must exit cleanly,
    not KeyError-traceback."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="missing key 'package'"):
        check.main()


def test_check_pyproject_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rtfm"\n')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="missing key 'project.version'"):
        check.main()


def test_check_deps_must_be_lists(tmp_path, monkeypatch):
    """A bare-string dependencies declaration would make set() compare character
    sets, so the guard must refuse it (uv itself rejects the shape anyway)."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = "0.5.1"\ndependencies = "mcp[cli]>=1.28.1,<2"\n'
    )
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


def test_check_missing_server_file_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "rtfm_server.py").unlink()
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="rtfm_server.py"):
        check.main()


def test_check_server_without_pep723_block_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path, script_deps="# plain module, no script block\n")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="PEP-723 block"):
        check.main()


def test_check_unparseable_pep723_block_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path, script_deps="# /// script\n# dependencies = [\n# ///\n")
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
    # The last subprocess call must be the self-check, run via the same Python.
    self_check = calls[-1][0]
    assert self_check[0] == sys.executable
    assert self_check[1].endswith("check_version_consistency.py")


def test_bump_refuses_on_pre_existing_drift(tmp_path, monkeypatch):
    make_tree(tmp_path, plugin_version="0.5.0")
    with pytest.raises(SystemExit, match="disagree"):
        run_bump(tmp_path, monkeypatch)
    # No-mutation guarantee: the refusal fires before any write, so a retry
    # after fixing the drift still sees the original versions.
    assert 'version = "0.5.1"' in (tmp_path / "pyproject.toml").read_text()
    assert '"version": "0.5.0"' in (tmp_path / ".claude-plugin" / "plugin.json").read_text()


def test_bump_rejects_non_pep440_version(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="usage"):
        run_bump(tmp_path, monkeypatch, new_version="not-a-version")


def test_bump_uv_lock_failure_is_clean_error_with_revert_instructions(tmp_path, monkeypatch):
    """A failing `uv lock` (a genuine resolution conflict — not the 0.5.1 class,
    where `uv lock` succeeded and `uv lock --check` caught the stale resolution)
    must exit with a message that owns the half-edited tree. pytest.raises
    (SystemExit) would fail on a raw CalledProcessError traceback instead."""
    make_tree(tmp_path)
    fake, _ = make_runner(lock_ok=False)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=fake)
    msg = str(exc.value.code)
    assert "already edited" in msg and "git checkout" in msg


def test_bump_uv_missing_is_clean_error(tmp_path, monkeypatch):
    """`uv` not on PATH raises FileNotFoundError from subprocess.run; the bump
    must exit cleanly, not traceback."""
    make_tree(tmp_path)

    def no_uv(args, **kwargs):
        raise FileNotFoundError("uv")

    with pytest.raises(SystemExit, match="uv.*not found"):
        run_bump(tmp_path, monkeypatch, runner=no_uv)


def test_bump_self_check_failure_aborts(tmp_path, monkeypatch):
    make_tree(tmp_path)
    fake, _ = make_runner(self_check_ok=1)
    with pytest.raises(SystemExit, match="post-bump consistency check failed"):
        run_bump(tmp_path, monkeypatch, runner=fake)


def test_bump_refuses_ambiguous_version_lines(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        (tmp_path / "pyproject.toml").read_text() + '\n[tool.other]\nversion = "0.5.1"\n'
    )
    with pytest.raises(SystemExit, match="expected exactly one"):
        run_bump(tmp_path, monkeypatch)


def test_bump_second_plan_refusal_leaves_first_file_untouched(tmp_path, monkeypatch):
    """If the plugin.json pattern can't match (say the file format shifted), the
    refusal must fire BEFORE pyproject.toml is written — otherwise a retry
    dead-ends on the drift refusal against the half-edited tree."""
    make_tree(tmp_path)
    # "version" : "0.5.1" (space before colon) — the line-anchored pattern
    # `^(\s*"version": ")` cannot match this.
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name" : "rtfm",\n  "version" : "0.5.1"\n}\n'
    )
    with pytest.raises(SystemExit, match="expected exactly one"):
        run_bump(tmp_path, monkeypatch)
    assert 'version = "0.5.1"' in (tmp_path / "pyproject.toml").read_text()


def test_bump_preserves_comma_when_version_is_mid_object(tmp_path, monkeypatch):
    """The real plugin.json has "version" as a middle key followed by a comma;
    the `,?` pattern must echo the comma back or the file becomes invalid JSON."""
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": "0.5.1",\n  "mcpServers": {}\n}\n'
    )
    fake, _ = make_runner()
    assert run_bump(tmp_path, monkeypatch, runner=fake) == 0
    text = (tmp_path / ".claude-plugin" / "plugin.json").read_text()
    assert '"version": "0.5.2",' in text
    assert json.loads(text)["version"] == "0.5.2"


def test_bump_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    """A pyproject.toml that parses but lacks [project].version must exit
    cleanly, not KeyError-traceback."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rtfm"\n')
    with pytest.raises(SystemExit, match="version"):
        run_bump(tmp_path, monkeypatch)
