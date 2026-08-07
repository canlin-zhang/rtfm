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
import os
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
    """Unpinned mcp in the PEP-723 block vs pinned in pyproject (the mirror
    direction of 0.5.1, where the block was the pinned side and pyproject
    lagged): exit 1, and the message shows the exact specifier from each file."""
    make_tree(tmp_path, script_deps=PEP723.replace('"mcp[cli]>=1.28.1,<2"', '"mcp[cli]"'))
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1
    out = capsys.readouterr()
    combined = out.out + out.err  # differing specifiers print to stdout, ERROR to stderr
    # Exact-line membership, not substrings: a swapped print loop would emit the
    # other side's specifier on each line, and substring matching can't tell —
    # "block: mcp[cli]" is a prefix of "block: mcp[cli]>=1.28.1,<2".
    lines = [line.strip() for line in combined.splitlines()]
    assert "only in rtfm_server.py PEP-723 block: mcp[cli]" in lines
    assert "only in pyproject.toml: mcp[cli]>=1.28.1,<2" in lines


def test_check_missing_rtfm_lock_entry_exits_1(tmp_path, monkeypatch, capsys):
    make_tree(tmp_path, lock_names=("other",))
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1
    assert "no [[package]] entry named 'rtfm'" in capsys.readouterr().err


def test_check_duplicate_rtfm_lock_entries_exit_1(tmp_path, monkeypatch, capsys):
    """Two rtfm entries — first-wins would silently pass on the first; the
    count guard must refuse."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "rtfm"\nversion = "0.5.1"\nsource = { virtual = "." }\n'
        '[[package]]\nname = "rtfm"\nversion = "9.9.9"\nsource = { virtual = "." }\n'
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1
    assert "2 [[package]] entries named 'rtfm'" in capsys.readouterr().err


def test_check_lock_missing_package_array_is_clean_error(tmp_path, monkeypatch):
    """A uv.lock that parses but has no [[package]] table must exit cleanly,
    not KeyError-traceback."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="missing key 'package'"):
        check.main()


def test_check_lock_package_wrong_shape_is_clean_error(tmp_path, monkeypatch):
    """`package = "rtfm"` parses as valid TOML; without the shape guard the
    entry lookup AttributeError-tracebacks on it. CI always runs against the
    well-formed real lock, so only a fixture can pin this."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text('package = "rtfm"\n')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_rtfm_entry_without_version_key_is_clean_error(tmp_path, monkeypatch, capsys):
    """An rtfm entry that exists but carries no version is a different failure
    than a missing entry — the message must distinguish the two."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "rtfm"\nsource = { virtual = "." }\n')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1
    assert "has no version key" in capsys.readouterr().err


def test_check_versions_must_be_strings(tmp_path, monkeypatch):
    """All three versions as equal ints, with deps matching on both sides:
    guard-less code compares equal and reports OK — the false-pass shape this
    suite exists to catch. (An empty pyproject deps list would exit 1 via deps
    drift instead, never reaching a verdict on the versions.)"""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = 5\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n'
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "rtfm"\nversion = 5\nsource = { virtual = "." }\n'
    )
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": 5\n}\n'
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_pyproject_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rtfm"\n')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="missing key 'project.version'"):
        check.main()


def test_check_deps_must_be_lists(tmp_path, monkeypatch):
    """Bare-string dependencies on BOTH sides is the discriminating fixture: with
    equal strings, set() compares character sets and reports OK — a false pass
    the guard must refuse. (Bare string on one side only is not discriminating;
    it drifts anyway and exits 1 regardless of the guard.)"""
    make_tree(tmp_path, script_deps='# /// script\n# dependencies = "mcp[cli]>=1.28.1,<2"\n# ///\n')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = "0.5.1"\ndependencies = "mcp[cli]>=1.28.1,<2"\n'
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_script_side_deps_guard_is_discriminating(tmp_path, monkeypatch):
    """Pyproject deps ["a"] (list) and a bare-string block "a": guard-less code
    compares set("a") == set(["a"]) and reports OK — only the script-side
    guard refuses. Pairs with test_check_deps_must_be_lists to pin each side
    of the two-list guard individually."""
    make_tree(tmp_path, script_deps='# /// script\n# dependencies = "a"\n# ///\n')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = "0.5.1"\ndependencies = ["a"]\n'
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_pyproject_side_deps_guard_is_discriminating(tmp_path, monkeypatch):
    """Pyproject deps "a" (bare string) and a block ["a"] (list): guard-less
    code compares set("a") == set(["a"]) and reports OK — only the project-side
    guard refuses. The mirror of the script-side test, pinning each side
    individually."""
    make_tree(tmp_path, script_deps='# /// script\n# dependencies = ["a"]\n# ///\n')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = "0.5.1"\ndependencies = "a"\n'
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


def test_check_server_as_directory_is_clean_error(tmp_path, monkeypatch):
    """rtfm_server.py as a directory: _read_pep723_deps's OSError catch must
    exit cleanly — the third read path in the same branch family."""
    make_tree(tmp_path)
    (tmp_path / "rtfm_server.py").unlink()
    (tmp_path / "rtfm_server.py").mkdir()
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="rtfm_server.py"):
        check.main()


def test_check_malformed_plugin_json_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """Garbage in plugin.json (check side): _read's JSONDecodeError branch must
    exit cleanly with 'unparseable', matching the TOML twin."""
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text('{"name": "rtfm",')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="unparseable"):
        check.main()


def test_check_non_table_project_is_clean_error(tmp_path, monkeypatch):
    """`project = 5` is valid TOML; without the isinstance guard the descent
    into it TypeErrors. The guard must exit cleanly."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text("project = 5\n")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="missing key"):
        check.main()


def test_check_pyproject_as_directory_is_clean_error(tmp_path, monkeypatch):
    """A directory where pyproject.toml belongs raises IsADirectoryError (an
    OSError) from read_text — the catch must exit cleanly. Directory fixtures
    work as root, so no skipif is needed."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").mkdir()
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="pyproject.toml"):
        check.main()


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


def test_check_non_utf8_file_is_clean_error(tmp_path, monkeypatch):
    """A non-UTF-8 file raises UnicodeDecodeError — a ValueError subclass, not
    OSError — and must exit cleanly with its own message, never traceback."""
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_bytes(b"\xff\xfe\x00")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="not UTF-8"):
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
        if args and args[:2] == ["uv", "lock"]:
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

    # The fake `uv lock` cannot regenerate uv.lock; simulate what the real
    # command does, then run the REAL check against the bumped tree — the
    # whole pipeline must agree.
    (tmp_path / "uv.lock").write_text(
        (tmp_path / "uv.lock").read_text().replace('version = "0.5.1"', 'version = "0.5.2"')
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 0


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
    assert "failed (exit 1)" in msg  # a genuine uv failure keeps its specific wording


def test_bump_uv_missing_is_clean_error(tmp_path, monkeypatch):
    """`uv` not on PATH raises FileNotFoundError from subprocess.run; the bump
    must exit cleanly (with the restore hint — the file writes precede uv) and
    never traceback."""
    make_tree(tmp_path)

    def no_uv(args, **kwargs):
        raise FileNotFoundError("uv")

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=no_uv)
    assert "missing from PATH" in str(exc.value.code)
    assert "git checkout" in str(exc.value.code)


def test_bump_uv_unrunnable_is_clean_error(tmp_path, monkeypatch):
    """uv present but not runnable: EACCES (missing +x bit, noexec mount)
    raises PermissionError; ENOEXEC (corrupt binary) raises plain OSError —
    both OSErrors, not CalledProcessError — and the bump must exit with the
    restore hint, never traceback."""
    make_tree(tmp_path)

    def noexec_uv(args, **kwargs):
        raise PermissionError(args[0])

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=noexec_uv)
    msg = str(exc.value.code)
    assert "git checkout" in msg
    assert "uv.lock" in msg  # RESTORE covers the regenerated lock too


def test_bump_uv_enoexec_is_clean_error(tmp_path, monkeypatch):
    """ENOEXEC (corrupt binary) raises plain OSError, not PermissionError —
    same branch, same restore hint."""
    make_tree(tmp_path)

    def enoexec_uv(args, **kwargs):
        raise OSError(8, "Exec format error", args[0])

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=enoexec_uv)
    msg = str(exc.value.code)
    assert "git checkout" in msg
    assert "uv.lock" in msg
    assert "resolution" not in msg  # ENOEXEC is not a resolution failure


def test_bump_interrupt_during_uv_lock_prints_restore_hint(tmp_path, monkeypatch):
    """Ctrl-C during `uv lock` (KeyboardInterrupt, not an OSError) leaves the
    hand-edited files at the new version with a stale lock — the exit must
    carry the restore hint, never a bare traceback."""
    make_tree(tmp_path)

    def interrupting_uv(args, **kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=interrupting_uv)
    msg = str(exc.value.code)
    assert "interrupted" in msg
    assert "git checkout" in msg


def test_bump_malformed_pyproject_is_clean_error(tmp_path, monkeypatch):
    """Garbage TOML in pyproject must exit cleanly from _toml_version, never
    traceback — the check script's twin is pinned, the bump's wasn't."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text("this is not [ toml")
    with pytest.raises(SystemExit, match=r"cannot read \[project\]\.version"):
        run_bump(tmp_path, monkeypatch)


def test_bump_malformed_plugin_json_is_clean_error(tmp_path, monkeypatch):
    """Invalid JSON in plugin.json (e.g. a hand-edit trailing comma) must exit
    cleanly from _json_version, never traceback."""
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text('{"name": "rtfm",')
    with pytest.raises(SystemExit, match=r'cannot read "version"'):
        run_bump(tmp_path, monkeypatch)


def test_bump_plugin_json_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text('{"name": "rtfm"}')
    with pytest.raises(SystemExit, match=r'cannot read "version"'):
        run_bump(tmp_path, monkeypatch)


def test_bump_plugin_json_version_must_be_string(tmp_path, monkeypatch):
    """pyproject version stays "0.5.1"; plugin.json's int version must be
    refused by the JSON-side str guard (if pyproject also had 5, the TOML
    guard would fire first — this fixture isolates the JSON side)."""
    make_tree(tmp_path)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": 5\n}\n'
    )
    with pytest.raises(SystemExit, match="must be a string"):
        run_bump(tmp_path, monkeypatch)


def test_bump_uv_lock_signal_death_is_clean_error(tmp_path, monkeypatch):
    """uv killed by a signal reports a negative returncode (e.g. -SIGINT) —
    that is not a resolution failure, and the message must say so instead of
    'Fix the resolution error'."""
    make_tree(tmp_path)

    def signaled(args, **kwargs):
        raise subprocess.CalledProcessError(-2, args)

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=signaled)
    msg = str(exc.value.code)
    assert "signal" in msg
    assert "git checkout" in msg
    assert "resolution" not in msg  # a signal death is not a resolution failure


def test_bump_pyproject_as_directory_is_clean_error(tmp_path, monkeypatch):
    """The bump's read of pyproject.toml hits the same OSError class (a
    directory where the file belongs); the check script's twin is pinned
    above, this pins the bump's _read_text branch."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").mkdir()
    with pytest.raises(SystemExit, match="pyproject.toml"):
        run_bump(tmp_path, monkeypatch)


def test_bump_self_check_failure_aborts(tmp_path, monkeypatch):
    make_tree(tmp_path)
    fake, _ = make_runner(self_check_ok=1)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=fake)
    msg = str(exc.value.code)
    assert "post-bump consistency check" in msg
    assert "git checkout" in msg  # restore hint: uv.lock is regenerated by now


def test_bump_self_check_unrunnable_is_clean_error(tmp_path, monkeypatch):
    """The self-check subprocess's OSError branch (sibling of the uv branch):
    it must exit with the restore hint, never traceback."""
    make_tree(tmp_path)

    def noexec_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0)
        raise PermissionError(args[0])

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=noexec_self_check)
    msg = str(exc.value.code)
    assert "self-check" in msg and "git checkout" in msg


def test_bump_check_script_missing_is_clean_error(tmp_path, monkeypatch):
    """The self-check's existence guard: without the check script next to the
    bump script, exit with the restore hint instead of a FileNotFoundError."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").unlink()
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch)
    assert "can't self-check" in str(exc.value.code)
    assert "git checkout" in str(exc.value.code)


def test_bump_check_script_as_directory_is_clean_error(tmp_path, monkeypatch):
    """A directory where the check script belongs: the readability probe fails
    (IsADirectoryError) and the bump exits with the restore hint — it must not
    blame a consistency failure the check never ran."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").unlink()
    (tmp_path / "scripts" / "check_version_consistency.py").mkdir()
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch)
    msg = str(exc.value.code)
    assert "self-check" in msg
    assert "git checkout" in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_bump_write_failure_is_clean_error_with_revert_instructions(tmp_path, monkeypatch):
    """A write failure (read-only file, disk full) after the first file was
    written must exit with a message that owns the half-edited tree — the
    plan-before-write guarantee covers refusals, not OS write errors."""
    make_tree(tmp_path)
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    plugin_json.chmod(0o444)
    try:
        with pytest.raises(SystemExit) as exc:
            run_bump(tmp_path, monkeypatch)
        msg = str(exc.value.code)
        assert "write failed" in msg and "git checkout" in msg
        assert "uv.lock" in msg  # RESTORE covers the regenerated lock too
        # The failure happened on the second write: pyproject is already at the
        # new version — the message must say so, or a retry dead-ends on drift.
        assert 'version = "0.5.2"' in (tmp_path / "pyproject.toml").read_text()
    finally:
        plugin_json.chmod(0o644)  # let pytest clean up the tmp dir


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


def test_bump_missing_pyproject_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    with pytest.raises(SystemExit, match="pyproject.toml not found"):
        run_bump(tmp_path, monkeypatch)


def test_bump_version_must_be_string(tmp_path, monkeypatch):
    """An unquoted `version = 5` would flow a list/int repr into the drift
    message; the reader must refuse the shape instead."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = 5\ndependencies = []\n'
    )
    with pytest.raises(SystemExit, match="must be a string"):
        run_bump(tmp_path, monkeypatch)


def test_bump_non_utf8_file_is_clean_error(tmp_path, monkeypatch):
    """UnicodeDecodeError is a ValueError subclass, not OSError — the read
    guard must name it explicitly."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SystemExit, match="not UTF-8"):
        run_bump(tmp_path, monkeypatch)
