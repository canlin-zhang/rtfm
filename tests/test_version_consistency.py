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
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )
    lock = "".join(
        f'[[package]]\nname = "{name}"\nversion = "{lock_version if name == "rtfm" else "0.1.0"}"\n'
        'source = { virtual = "." }\n'
        for name in lock_names
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    (tmp_path / ".claude-plugin").mkdir(parents=True)
    # Deliberately NOT the real-file shape: here "version" is the last key with no
    # trailing comma, so the `,?` pattern's empty branch is the default exercise.
    # test_bump_preserves_comma_when_version_is_mid_object covers the real shape.
    # Explicit UTF-8 on every write: fixtures may carry em-dashes, and the
    # suite must be hermetic under an ASCII locale (the environment the
    # ASCII-locale tests simulate for their children).
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "rtfm", "version": plugin_version}, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "rtfm_server.py").write_text(script_deps, encoding="utf-8")
    # The real repo carries the check script next to the bump script; the
    # bump's self-check guard needs the skeleton to mirror that.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_version_consistency.py").write_text("# stub\n", encoding="utf-8")


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
    exit cleanly — the server-side twin of the two pyproject-as-directory
    tests."""
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


def test_check_non_utf8_server_is_clean_error(tmp_path, monkeypatch):
    """Same branch family, third reader pinned here: _read_pep723_deps's
    UnicodeDecodeError catch — a non-UTF-8 rtfm_server.py must exit with
    'not UTF-8 text', never traceback."""
    make_tree(tmp_path)
    (tmp_path / "rtfm_server.py").write_bytes(b"\xff\xfe\x00")
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


def test_check_reversed_pep723_markers_is_clean_error(tmp_path, monkeypatch):
    """Markers in reversed order (a '# ///' line before '# /// script') hit the
    end <= start half of the block guard — without it, the slice is empty and
    the error is misattributed to a missing 'dependencies' key."""
    make_tree(tmp_path, script_deps="# ///\n# /// script\n")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="PEP-723 block"):
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
            return subprocess.CompletedProcess(args, 0, stdout="")
        # The self-check script: exit 0 must carry the OK verdict line — the
        # bump's gate requires it, so the fake models the real check's contract.
        return subprocess.CompletedProcess(
            args, self_check_ok, stdout=(
                "OK: version and dependency declarations are consistent"
                if self_check_ok == 0
                else ""
            )
        )

    return fake, calls


def run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=None):
    monkeypatch.setattr(bump, "ROOT", tmp_path)
    monkeypatch.setattr(bump.subprocess, "run", runner if runner is not None else make_runner()[0])
    monkeypatch.setattr(sys, "argv", ["bump_version.py", new_version])
    return bump.main()


def run_bump_in_subprocess(
    tmp_path, timeout=10, self_check_timeout=None, env_extra=None, require_ascii=False
):
    """Run the real bump script in a child process with a fake `uv` on PATH —
    for fixtures where the bump would otherwise hang (a regressed guard against
    a FIFO) or where only a real subprocess exercises the decode path (a
    garbage-emitting check script — a mocked runner returns a CompletedProcess
    without decoding). self_check_timeout overrides the bump's bound (default
    the module's SELF_CHECK_TIMEOUT) so a real hanging child times out in test
    time; env_extra layers locale overrides for the ASCII-locale tests."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(0o755)
    if require_ascii and (
        sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8"
    ):
        # On an ASCII host the child inherits ASCII anyway and the child-side
        # assert passes vacuously — the pin would be undetectable. On macOS/
        # Windows the fs encoding stays utf-8 under LC_ALL=C and the child
        # assert can never pass (it always raises). Either way the pin is
        # meaningless: skip loudly.
        pytest.skip("require_ascii pins need a Linux UTF-8 host")
    loader = (
        "import importlib.util, sys; from pathlib import Path; "
        "assert sys.argv[4] == '0' or sys.getfilesystemencoding() == 'ascii', "
        "'env_extra must reach the child'; "
        "s = importlib.util.spec_from_file_location('bump', sys.argv[1]); "
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.ROOT = Path(sys.argv[2]); "
        "m.SELF_CHECK_TIMEOUT = int(sys.argv[3]); "
        "sys.argv = ['bump_version.py', '0.5.2']; sys.exit(m.main())"
    )
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    if env_extra:
        env.update(env_extra)
    bound = max(
        1, self_check_timeout if self_check_timeout is not None else bump.SELF_CHECK_TIMEOUT
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            loader,
            str(ROOT / "scripts/bump_version.py"),
            str(tmp_path),
            str(bound),
            "1" if require_ascii else "0",
        ],
        timeout=timeout,
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
    )


def run_check_in_subprocess(tmp_path, env_extra=None, timeout=10, require_ascii=False):
    """Run the real check script against a fixture tree in a child process
    (ROOT redirected), optionally under a hostile locale."""
    if require_ascii and (
        sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8"
    ):
        pytest.skip("require_ascii pins need a Linux UTF-8 host")
    loader = (
        "import importlib.util, sys; from pathlib import Path; "
        "assert sys.argv[3] == '0' or sys.getfilesystemencoding() == 'ascii', "
        "'env_extra must reach the child'; "
        "s = importlib.util.spec_from_file_location('check', sys.argv[1]); "
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.ROOT = Path(sys.argv[2]); sys.exit(m.main())"
    )
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            loader,
            str(ROOT / "scripts/check_version_consistency.py"),
            str(tmp_path),
            "1" if require_ascii else "0",
        ],
        timeout=timeout,
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
    )


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
    assert self_check[1] == "-u"  # unbuffered: a replaced script's clue must arrive
    assert self_check[2].endswith("check_version_consistency.py")

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
    assert "resolution" in msg  # ...and its specific advice (the positive twin of the signal pin)


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
    # The normal path keeps the hedge — these three asserts pin the KI
    # condition's lock_only conjunct (a dropped conjunct would advise the
    # lock-only message here, where the files genuinely were edited).
    assert "may be edited" in msg
    assert "were NOT modified" not in msg
    assert "git checkout pyproject.toml" in msg


def test_bump_interrupt_mid_write_lock_only_hedge(tmp_path, monkeypatch):
    """A Ctrl-C during the writes in the lock-only path is a real truncation
    hazard — the handler cannot distinguish mid-write from between the two
    byte-identical writes (as simulated here, the interrupt fires after the
    real first write) — so it must keep the hedge, not claim the files were
    NOT modified (pins the KI condition's writes_completed conjunct)."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")
    real_write_text = Path.write_text

    def interrupt_on_first_write(self, *args, **kwargs):
        real_write_text(self, *args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(bump.Path, "write_text", interrupt_on_first_write)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2")
    msg = str(exc.value.code)
    assert "may be edited" in msg
    assert "were NOT modified" not in msg
    assert "git checkout pyproject.toml" in msg


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
    assert "then retry: python3 scripts/bump_version.py 0.5.2" in msg  # the normal-path gate retry


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
    """The self-check guard's missing-script branch: without the check script
    next to the bump script, exit with the restore hint instead of a
    FileNotFoundError."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").unlink()
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch)
    assert "can't self-check" in str(exc.value.code)
    assert "git checkout" in str(exc.value.code)
    assert "not found" in str(exc.value.code)  # the stat() FileNotFoundError fires first


def test_bump_check_script_as_directory_is_clean_error(tmp_path, monkeypatch):
    """A directory where the check script belongs: the S_ISREG guard rejects
    it and the bump exits with the restore hint — it must not blame a
    consistency failure the check never ran."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").unlink()
    (tmp_path / "scripts" / "check_version_consistency.py").mkdir()
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch)
    msg = str(exc.value.code)
    assert "self-check" in msg
    assert "git checkout" in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_bump_check_script_parent_unreadable_is_clean_error(tmp_path, monkeypatch):
    """An unreadable scripts/ directory makes stat() raise PermissionError —
    the guard must exit cleanly, never traceback."""
    make_tree(tmp_path)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.chmod(0o000)
    try:
        with pytest.raises(SystemExit) as exc:
            run_bump(tmp_path, monkeypatch)
        msg = str(exc.value.code)
        assert "cannot be accessed" in msg
        assert "git checkout" in msg
    finally:
        scripts_dir.chmod(0o755)  # let pytest clean up the tmp dir


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_bump_check_script_unreadable_is_clean_error(tmp_path, monkeypatch):
    """The canonical probe case: a chmod-0 check script (non-root) must exit
    with 'unreadable', never the generic gate message — the directory variant
    above pins the same guard root-safely."""
    make_tree(tmp_path)
    check_script = tmp_path / "scripts" / "check_version_consistency.py"
    check_script.chmod(0o000)
    try:
        with pytest.raises(SystemExit) as exc:
            run_bump(tmp_path, monkeypatch)
        msg = str(exc.value.code)
        assert "unreadable" in msg
        assert "git checkout" in msg
    finally:
        check_script.chmod(0o644)  # let pytest clean up the tmp dir


def test_bump_timeout_echoes_unflushed_child_output(tmp_path):
    """A replaced check script that prints a clue then hangs without flushing
    (stdout to a pipe is block-buffered) must still have its clue echoed: the
    child runs with -u. Without it, TimeoutExpired.output comes back None and
    the echo is silent."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "import time\nprint('CLUE')\ntime.sleep(1000)\n"
    )
    # self_check_timeout=0 makes the harness's max(1, ...) clamp load-bearing:
    # an unclamped 0 raises TimeoutExpired instantly, output is None, and the
    # CLUE never reaches the echo.
    proc = run_bump_in_subprocess(tmp_path, self_check_timeout=0)
    assert "CLUE" in proc.stdout  # the clue survived the pipe, unflushed
    assert "timed out" in proc.stdout + proc.stderr


def test_bump_timeout_silent_child_prints_no_none(tmp_path):
    """A check script that hangs without printing: the timeout handler must
    not print 'None' — exercises the falsy-output guard through the real
    timeout handler (output is None when nothing was read, so the decode
    branch is skipped)."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "import time\ntime.sleep(1000)\n"
    )
    proc = run_bump_in_subprocess(tmp_path, self_check_timeout=1)
    combined = proc.stdout + proc.stderr
    assert "timed out" in combined
    assert "None" not in combined


def test_check_prints_drift_under_ascii_locale(tmp_path):
    """Non-ASCII version values must not crash the check's drift listing
    under an ASCII locale — the check's stdout reconfigure (the bump got the
    same guard in an earlier round) must keep the report printing."""
    # The three fixture values (0.5.1 / 0.5.2 / 0.5.3) must stay mutually
    # non-prefix: a value like 0.5.10 would make '0.5.1' match its rendering
    # and silently weaken the pins below.
    make_tree(tmp_path, lock_version="0.5.2—β")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = "0.5.1"\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )
    # A third distinct value for plugin.json: with only two distinct prefixes,
    # a lock<->plugin swap of the shared value rendered identically and passed
    # every pin; three distinct prefixes make all six permutations
    # distinguishable. (lock_version= above feeds only uv.lock — pyproject and
    # plugin.json are overwritten right here.)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": "0.5.3—β"\n}\n',
        encoding="utf-8",
    )
    proc = run_check_in_subprocess(
        tmp_path,
        env_extra={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        require_ascii=True,
    )
    combined = proc.stdout + proc.stderr
    assert "UnicodeEncodeError" not in combined  # this assert rules the crash out
    # Belt-and-braces: the content pins catch pre-verdict crashes; this one
    # documents 'no traceback' as an independent contract.
    assert "Traceback" not in combined
    assert "pyproject.toml: 0.5.1" in proc.stdout  # the drift listing itself printed
    # All three listing lines are pinned by their ASCII-safe prefixes, with a
    # DISTINCT prefix per file (0.5.1 / 0.5.2 / 0.5.3) so any mis-associated
    # value or swapped entry fails the pins. The non-ASCII tails render as
    # '??' on the reconfigured stdout — unpinned.
    assert "uv.lock (rtfm entry): 0.5.2" in proc.stdout
    assert ".claude-plugin/plugin.json: 0.5.3" in proc.stdout
    # The verdict is stderr and the data lines stdout — placement is part of
    # the pin, matching the suite's documented convention.
    assert "differs across files" in proc.stderr
    assert "run scripts/bump_version.py" in proc.stderr  # the actionable half
    assert proc.returncode == 1


def test_check_pep723_block_without_deps_key_is_clean_error(tmp_path, monkeypatch):
    """A PEP-723 block that parses but lacks 'dependencies' hits _key's
    missing-key exit — clean, never a traceback."""
    make_tree(tmp_path, script_deps='# /// script\n# requires-python = ">=3.11"\n# ///\n')
    monkeypatch.setattr(check, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="missing key"):
        check.main()


def test_bump_usage_wrong_arg_count_is_clean_error(tmp_path, monkeypatch):
    """A wrong argument count exits with the usage message, never a
    traceback. The argv guard fires before any file access, so no tree is
    needed — ROOT points at a nonexistent path, and a guard moved below the
    reads would fail with a file error instead of 'usage'. The match is the
    full usage line: a bare 'usage' would also match the tmp_path directory
    name inside the file-error message."""
    monkeypatch.setattr(bump, "ROOT", tmp_path / "nonexistent")
    monkeypatch.setattr(sys, "argv", ["bump_version.py"])  # no version argument
    with pytest.raises(SystemExit, match=r"usage: python3 scripts/bump_version\.py"):
        bump.main()


def test_bump_same_version_refuses(tmp_path, monkeypatch):
    """Bumping to the current version is a no-op that would print a
    misleading 'Bumped' with a commit suggestion — refuse instead, before any
    subprocess runs."""
    make_tree(tmp_path)
    fake, calls = make_runner()
    with pytest.raises(SystemExit, match="already at version"):
        run_bump(tmp_path, monkeypatch, new_version="0.5.1", runner=fake)
    assert calls == []  # refused before any subprocess call


def test_bump_same_version_with_stale_lock_proceeds(tmp_path, monkeypatch, capsys):
    """pyproject and plugin.json at the target with a lagging uv.lock (a
    failed earlier `uv lock`) is NOT a no-op — the bump must proceed so
    `uv lock` can bring the lock up; the refusal fires only when the lock
    agrees too. The success line uses lock-only framing, not a misleading
    'Bumped' with release framing."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")
    fake, calls = make_runner()
    assert run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=fake) == 0
    assert ["uv", "lock"] in [a for a, _ in calls]  # the lock catch-up ran uv lock
    out = capsys.readouterr().out
    assert "uv.lock regenerated at 0.5.2" in out
    assert "chore: refresh lockfile" in out
    assert "release: bump to 0.5.2" not in out


def test_bump_uv_lock_failure_lock_only_advice(tmp_path, monkeypatch):
    """In the lock-only path a uv failure must say the files were NOT modified
    and restore only uv.lock — the destructive full-checkout advice is the
    regression this pins."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")
    fake, _ = make_runner(lock_ok=False)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=fake)
    msg = str(exc.value.code)
    assert "were NOT modified" in msg
    assert "git checkout uv.lock" in msg
    assert "git checkout pyproject.toml" not in msg


def test_bump_uv_lock_timeout_lock_only(tmp_path, monkeypatch):
    """The uv-lock timeout branch in the lock-only path: a clean message with
    the lock-only clause; the 600s bound is asserted like the self-check's."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")

    def timeout_uv(args, **kwargs):
        assert kwargs.get("timeout") == 600, "uv lock must be bounded"
        raise subprocess.TimeoutExpired(args, 600)

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=timeout_uv)
    msg = str(exc.value.code)
    assert "timed out after 600s" in msg
    assert "were NOT modified" in msg
    assert "git checkout pyproject.toml" not in msg


def test_bump_self_check_failure_lock_only_advice(tmp_path, monkeypatch):
    """The lock-only gate fires for two indistinguishable classes — the check
    ran and reported drift (echoed above), or the check script was replaced
    and silently exited 0 — so the advice must hedge both and never suggest
    checking out files the bump never modified."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")
    fake, _ = make_runner(self_check_ok=1)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=fake)
    msg = str(exc.value.code)
    assert "fix the drift shown above" in msg
    assert "restore it first" in msg  # the replaced-script hedge
    assert "git checkout scripts/check_version_consistency.py" in msg
    assert "git checkout pyproject.toml" not in msg
    assert "git checkout uv.lock" not in msg


@pytest.mark.parametrize(
    "break_check_script",
    [
        pytest.param(
            lambda t: (t / "scripts" / "check_version_consistency.py").unlink(),
            id="missing",
        ),
        pytest.param(
            lambda t: (t / "scripts" / "check_version_consistency.py").unlink()
            or (t / "scripts" / "check_version_consistency.py").mkdir(),
            id="directory",
        ),
        pytest.param(
            lambda t: (t / "scripts" / "check_version_consistency.py").chmod(0o000),
            id="unreadable-file",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions"),
        ),
        pytest.param(
            lambda t: (t / "scripts").chmod(0o000),
            id="unreadable-parent",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions"),
        ),
    ],
)
def test_bump_self_check_script_problem_lock_only_advice(
    tmp_path, monkeypatch, break_check_script
):
    """Every post-write self-check branch in the lock-only path (missing,
    directory, unreadable file or parent) must name the check script as the
    problem and never suggest reverting uv.lock or the unmodified files."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")
    scripts_dir = tmp_path / "scripts"
    check_script = scripts_dir / "check_version_consistency.py"
    try:
        break_check_script(tmp_path)
        fake, _ = make_runner()
        with pytest.raises(SystemExit) as exc:
            run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=fake)
        msg = str(exc.value.code)
        assert "restore it first" in msg
        assert "git checkout scripts/check_version_consistency.py" in msg
        assert "git checkout pyproject.toml" not in msg
        assert "git checkout uv.lock" not in msg
    finally:
        # chmod-0 paths defeat pytest's rename-based cleanup — restore them.
        scripts_dir.chmod(0o755)
        if check_script.exists():
            check_script.chmod(0o644)


def test_bump_self_check_unrunnable_lock_only_advice(tmp_path, monkeypatch):
    """The run-OSError self-check branch in the lock-only path follows the
    same check-script advice."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")

    def noexec_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        raise PermissionError(args[0])

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=noexec_self_check)
    msg = str(exc.value.code)
    assert "restore it first" in msg
    assert "git checkout scripts/check_version_consistency.py" in msg
    assert "git checkout pyproject.toml" not in msg
    assert "git checkout uv.lock" not in msg


def test_bump_interrupt_lock_only_advice(tmp_path, monkeypatch):
    """A Ctrl-C after the writes complete in the lock-only path must not
    claim the files were edited or advise checking them out."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")

    def interrupting_uv(args, **kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=interrupting_uv)
    msg = str(exc.value.code)
    assert "were NOT modified" in msg
    assert "git checkout uv.lock" in msg
    assert "git checkout pyproject.toml" not in msg


# The whitespace variants (not "") are the load-bearing ones: only they
# discriminate a strip-removal regression. "\\t" is written as the escape so
# both TOML and JSON parse it to a tab (a raw tab would break the JSON case).
@pytest.mark.parametrize("blank_version", ["", "   ", "\\t"])
def test_check_blank_versions_are_clean_error(tmp_path, monkeypatch, blank_version):
    """Blank versions (empty or whitespace-only) in all three files would
    pass the isinstance guard and set-equality as 'OK' — the guard must
    refuse the shape and name the offending files."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "rtfm"\nversion = "{blank_version}"\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "rtfm"\nversion = "{blank_version}"\n'
        'source = { virtual = "." }\n',
        encoding="utf-8",
    )
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        f'{{"name": "rtfm", "version": "{blank_version}"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.main() == 1


@pytest.mark.parametrize(
    "lock_content",
    [
        pytest.param(None, id="missing"),
        pytest.param("this is not [ toml", id="unparseable"),
        pytest.param("version = 1\n", id="no-package-key"),
        pytest.param('package = "rtfm"\n', id="bare-string-package"),
        pytest.param('package = ["rtfm"]\n', id="list-of-strings-package"),
        pytest.param(
            '[[package]]\nname = "rtfm"\nversion = "0.5.2"\nsource = { virtual = "." }\n'
            '[[package]]\nname = "rtfm"\nversion = "0.5.1"\nsource = { virtual = "." }\n',
            id="duplicate-rtfm-entries",
        ),
        pytest.param(
            '[[package]]\nname = "other"\nversion = "0.5.1"\n'
            'source = { virtual = "." }\n',
            id="no-rtfm-entry",
        ),
        pytest.param(b"\xff\xfe\x00", id="non-utf8-bytes"),
    ],
)
def test_bump_same_version_with_malformed_lock_proceeds(
    tmp_path, monkeypatch, lock_content
):
    """A lock that cannot confirm the version (missing, unparseable,
    wrong-shape, duplicated, or non-UTF-8) must not traceback while deciding
    whether to refuse — the bump proceeds so `uv lock` can bring the lock
    up."""
    make_tree(tmp_path, version="0.5.2")
    if lock_content is None:
        (tmp_path / "uv.lock").unlink()
    elif isinstance(lock_content, bytes):
        (tmp_path / "uv.lock").write_bytes(lock_content)
    else:
        (tmp_path / "uv.lock").write_text(lock_content, encoding="utf-8")
    fake, _ = make_runner()
    assert run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=fake) == 0


def test_check_reads_utf8_under_ascii_locale(tmp_path):
    """The explicit UTF-8 reads must keep working under an ASCII locale: the
    repo files carry em-dashes, and locale read_text would mislabel a valid
    UTF-8 file 'not UTF-8'. The fixture puts an em-dash in plugin.json and in
    rtfm_server.py's PEP-723 block, covering _read and _read_pep723_deps."""
    make_tree(
        tmp_path,
        script_deps=(
            "# /// script\n"
            '# requires-python = ">=3.11"\n'
            '# description = "test — em dash"\n'
            "# dependencies = [\n"
            '#   "mcp[cli]>=1.28.1,<2",\n#   "pymupdf",\n#   "pypdf",\n# ]\n'
            "# ///\n"
        ),
    )
    plugin_json = (
        '{\n  "name": "rtfm",\n  "description": "test — em dash",\n  "version": "0.5.1"\n}\n'
    )
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(plugin_json, encoding="utf-8")
    proc = run_check_in_subprocess(
        tmp_path,
        env_extra={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        require_ascii=True,
    )
    assert proc.returncode == 0
    assert "OK" in proc.stdout


def test_bump_writes_utf8_under_ascii_locale(tmp_path):
    """The explicit UTF-8 writes must survive an ASCII locale: pyproject and
    plugin.json carry em-dashes, and a locale-encoding write would truncate
    the file in open('w') before UnicodeEncodeError — a ValueError outside
    the OSError handler."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm"\nversion = "0.5.1"\ndescription = "test — em dash"\n'
        'dependencies = [\n    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "description": "test — em dash",\n  "version": "0.5.1"\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "print('OK: version and dependency declarations are consistent')\n"
    )
    proc = run_bump_in_subprocess(
        tmp_path,
        env_extra={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        require_ascii=True,
    )
    combined = proc.stdout + proc.stderr
    assert "UnicodeEncodeError" not in combined
    assert "Bumped" in combined
    # File state, not just output: both writes landed intact under ASCII.
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.5.2"' in pyproject
    assert "test — em dash" in pyproject  # the U+2014 glyph survived the write
    plugin_text = (tmp_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    assert '"version": "0.5.2"' in plugin_text
    assert "test — em dash" in plugin_text  # the U+2014 glyph survived, not mangled
    plugin = json.loads(plugin_text)
    assert plugin["version"] == "0.5.2"


def test_bump_check_script_as_fifo_is_clean_error(tmp_path):
    """A FIFO at the check-script path must exit cleanly — a type check
    regressed to a plain existence check hangs the child's open() forever,
    which the timeout fails loudly instead of hanging the suite."""
    make_tree(tmp_path)
    fifo = tmp_path / "scripts" / "check_version_consistency.py"
    fifo.unlink()
    os.mkfifo(fifo)
    proc = run_bump_in_subprocess(tmp_path)
    combined = proc.stdout + proc.stderr
    assert "is not a regular file" in combined
    assert proc.returncode == 1  # the FIFO error exits 1 end to end


def test_bump_check_script_garbage_stdout_exits_1(tmp_path):
    """A replaced check script emitting raw bytes must not traceback the bump
    with a UnicodeDecodeError — errors='replace' routes it to the gate's clean
    'did not pass' message."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe')\n"
    )
    # Under an ASCII locale the echoed U+FFFD would crash the bump's stdout
    # without the sys.stdout.reconfigure(errors="replace") guard — this run
    # makes that guard load-bearing.
    proc = run_bump_in_subprocess(
        tmp_path,
        env_extra={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        require_ascii=True,
    )
    combined = proc.stdout + proc.stderr
    assert "UnicodeDecodeError" not in combined
    assert "did not pass" in combined
    # The gate echoed the decoded garbage, on its own line — pins the echo and
    # the append-newline half of its newline handling. The pipe decode is
    # locale-based, but ASCII and UTF-8 decoding of \xff\xfe with
    # errors="replace" both yield two U+FFFD; the child's ASCII-forced stdout
    # then renders them as '?' here on every host.
    assert "??\n" in proc.stdout


def test_bump_check_script_trailing_newline_stdout_no_double_newline(tmp_path):
    """A check script whose stdout ends with a newline (the real check's
    shape): the echo must not add a second one — pins the end='' half of the
    newline handling, which the no-trailing-newline fixture cannot reach."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe\\n')\n"
    )
    proc = run_bump_in_subprocess(tmp_path)
    # Echoed once, on its own line — the U+FFFD renders as '?' under an ASCII
    # child stdout, so accept either rendering (mirrors the garbage test).
    assert "��\n" in proc.stdout or "??\n" in proc.stdout
    assert "��\n\n" not in proc.stdout and "??\n\n" not in proc.stdout  # no double newline


def test_bump_interrupt_during_self_check_is_clean_error(tmp_path, monkeypatch):
    """Ctrl-C during the self-check (the bump already completed): the tree is
    consistent, so the message points at verification, not restoration."""
    make_tree(tmp_path)

    def interrupt_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        raise KeyboardInterrupt()

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=interrupt_self_check)
    msg = str(exc.value.code)
    assert "self-check" in msg
    assert "check_version_consistency.py" in msg
    assert "uv.lock was regenerated" in msg  # the honest claim, not an agreement one
    assert "agree" not in msg  # the check was interrupted before it could verify


def test_bump_self_check_timeout_is_clean_error(tmp_path, monkeypatch, capsys):
    """A check script that hangs (e.g. replaced by a loop) must hit the 60s
    bound as a clean timeout error, never a silent hang — and its partial
    output must be echoed (TimeoutExpired.output is bytes even with
    text=True; the handler must decode before printing)."""
    make_tree(tmp_path)

    def hanging_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        assert kwargs.get("timeout") == 60, "self-check must be bounded"
        raise subprocess.TimeoutExpired(args, 60, output=b"PARTIAL-OUTPUT-LINE\n")

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=hanging_self_check)
    msg = str(exc.value.code)
    assert "timed out" in msg
    assert "Restore it first" in msg  # a replaced script must be restored, not re-run
    assert "uncommitted edits" in msg  # the destructive side of git checkout is named
    assert "verify with" in msg
    out = capsys.readouterr().out
    assert "PARTIAL-OUTPUT-LINE\n" in out  # the partial output was echoed
    assert "PARTIAL-OUTPUT-LINE\n\n" not in out  # no double newline


def test_bump_self_check_timeout_partial_output_no_newline(tmp_path, monkeypatch, capsys):
    """The append-newline half of the timeout echo: partial output without a
    trailing newline must get one, mirroring the gate echo's two fixtures."""
    make_tree(tmp_path)

    def hanging_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        raise subprocess.TimeoutExpired(args, 60, output=b"PARTIAL")

    with pytest.raises(SystemExit):
        run_bump(tmp_path, monkeypatch, runner=hanging_self_check)
    out = capsys.readouterr().out
    assert "PARTIAL\n" in out


def test_bump_self_check_exit0_without_ok_line_exits_1(tmp_path, monkeypatch):
    """A check script that exits 0 without the OK verdict (e.g. replaced by an
    empty file — a /dev/null symlink is already rejected by the S_ISREG
    guard) must not count as a pass — the gate requires the OK line, not just
    exit 0."""
    make_tree(tmp_path)

    def silent_pass(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="")

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=silent_pass)
    msg = str(exc.value.code)
    assert "did not pass" in msg
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
