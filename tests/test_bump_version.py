# tests/test_bump_version.py
"""Contract tests for scripts/bump_version.py."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.version_tree import (
    ROOT,
    _load,
    make_tree,
    run_bump_in_subprocess,
    run_check,
)

bump = _load("bump_version", ROOT / "scripts/bump_version.py")


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
                "OK: version declarations are consistent"
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



def test_bump_updates_all_declarations_and_self_checks(tmp_path, monkeypatch):
    make_tree(tmp_path)
    fake, calls = make_runner()
    assert run_bump(tmp_path, monkeypatch, runner=fake) == 0

    assert 'version = "0.5.2"' in (tmp_path / "pyproject.toml").read_text()
    # plugin.json's "version" is the LAST key in the fixture (no trailing comma);
    # the result must still be valid JSON — the comma-injection regression test.
    plugin = json.loads((tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == "0.5.2"
    opencode = json.loads((tmp_path / "plugins/opencode" / "package.json").read_text())
    assert opencode["version"] == "0.5.2"

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
    assert run_check(tmp_path, monkeypatch) == 0


def test_bump_refuses_on_pre_existing_drift(tmp_path, monkeypatch):
    make_tree(tmp_path, plugin_version="0.5.0")
    with pytest.raises(SystemExit, match="disagree"):
        run_bump(tmp_path, monkeypatch)
    # No-mutation guarantee: the refusal fires before any write, so a retry
    # after fixing the drift still sees the original versions.
    assert 'version = "0.5.1"' in (tmp_path / "pyproject.toml").read_text()
    assert '"version": "0.5.0"' in (
        tmp_path / "plugins/claude/.claude-plugin" / "plugin.json"
    ).read_text()


def test_bump_refuses_on_opencode_drift(tmp_path, monkeypatch):
    make_tree(tmp_path, opencode_version="0.5.0")
    with pytest.raises(SystemExit, match="disagree"):
        run_bump(tmp_path, monkeypatch)


def test_bump_rejects_non_pep440_version(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="usage"):
        run_bump(tmp_path, monkeypatch, new_version="not-a-version")


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


@pytest.mark.parametrize(
    "lock_content",
    [
        pytest.param(None, id="missing"),
        pytest.param("this is not [ toml", id="unparseable"),
        pytest.param("version = 1\n", id="no-package-key"),
        pytest.param('package = "rtfm"\n', id="bare-string-package"),
        pytest.param('package = ["rtfm"]\n', id="list-of-strings-package"),
        pytest.param(
            '[[package]]\nname = "rtfm-mcp"\nversion = "0.5.2"\nsource = { virtual = "." }\n'
            '[[package]]\nname = "rtfm-mcp"\nversion = "0.5.1"\nsource = { virtual = "." }\n',
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


def test_bump_uv_lock_failure_is_clean_error_with_revert_instructions(tmp_path, monkeypatch):
    """A failing `uv lock` (a genuine resolution conflict — not the 0.5.1 class,
    where `uv lock` succeeded and `uv lock --check` caught the stale resolution)
    must exit with a message that owns the half-edited tree. pytest.raises
    (SystemExit) would fail on a raw CalledProcessError traceback instead."""
    make_tree(tmp_path)

    def failing_uv(args, **kwargs):
        assert kwargs.get("timeout") == 600, "uv lock must be bounded"
        raise subprocess.CalledProcessError(1, args)

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=failing_uv)
    msg = str(exc.value.code)
    assert "failed (exit 1)" in msg  # a genuine uv failure keeps its specific wording
    assert "already edited" in msg and "git checkout" in msg


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


def test_bump_uv_missing_is_clean_error(tmp_path, monkeypatch):
    """`uv` not on PATH raises FileNotFoundError from subprocess.run — an
    OSError; the bump must exit cleanly (with the restore hint — the file
    writes precede uv) and never traceback."""
    make_tree(tmp_path)

    def no_uv(args, **kwargs):
        raise FileNotFoundError("uv")

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=no_uv)
    msg = str(exc.value.code)
    assert "could not run `uv lock`" in msg
    assert "git checkout" in msg


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
    assert "may be edited" in msg  # the hedge: the interrupt could be mid-write
    assert "git checkout" in msg


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda args: subprocess.CalledProcessError(1, args), id="uv-failure"),
        pytest.param(lambda args: KeyboardInterrupt(), id="interrupt"),
    ],
)
def test_bump_lock_only_advice_restores_only_lock(tmp_path, monkeypatch, failure):
    """In the lock-only path a failure (uv failure or Ctrl-C) must say the
    files were NOT modified and restore only uv.lock — the destructive
    full-checkout advice is the regression this pins."""
    make_tree(tmp_path, version="0.5.2", lock_version="0.5.1")

    def failing(args, **kwargs):
        raise failure(args)

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, new_version="0.5.2", runner=failing)
    msg = str(exc.value.code)
    assert "were NOT modified" in msg
    assert "git checkout uv.lock" in msg
    assert "git checkout pyproject.toml" not in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_bump_write_failure_is_clean_error_with_revert_instructions(tmp_path, monkeypatch):
    """A write failure (read-only file, disk full) after the first file was
    written must exit with a message that owns the half-edited tree — the
    plan-before-write guarantee covers refusals, not OS write errors."""
    make_tree(tmp_path)
    plugin_json = tmp_path / "plugins/claude/.claude-plugin" / "plugin.json"
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


def test_bump_self_check_failure_aborts(tmp_path, monkeypatch):
    """The gate's ran-and-failed class: the bump's own edits are consistent
    (the check only compares declared strings, which the bump kept in sync),
    so the advice restores nothing of the bump's — it names the drift or the
    check script and points at verification. Same advice in both modes, so
    this one pin covers the lock-only path too."""
    make_tree(tmp_path)
    fake, _ = make_runner(self_check_ok=1)
    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=fake)
    msg = str(exc.value.code)
    assert "post-bump consistency check" in msg
    assert "The bump's own edits are consistent" in msg
    assert "Fix the drift" in msg
    assert "git checkout scripts/check_version_consistency.py" in msg
    assert "git checkout pyproject.toml" not in msg
    assert "verify with" in msg


def test_bump_interrupt_mid_write_lock_only_hedge(tmp_path, monkeypatch):
    """A Ctrl-C during the writes in the lock-only path is a real truncation
    hazard (open('w') truncates before writing): the files are byte-identical
    only after BOTH writes complete, so the interrupt message must keep the
    'may be edited' hedge with the full restore — never claim the files were
    NOT modified."""
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


def test_bump_self_check_exit0_without_ok_line_exits_1(tmp_path, monkeypatch):
    """A check script that exits 0 without the OK verdict (e.g. replaced or
    truncated — an empty file exits 0 silently) must not count as a pass: the
    gate requires the OK line, not just exit 0, and the advice restores the
    check script — never the bump's own files."""
    make_tree(tmp_path)

    def silent_pass(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="")

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=silent_pass)
    msg = str(exc.value.code)
    assert "did not pass" in msg
    assert "Restore the check script with: git checkout scripts/check_version_consistency.py" in msg
    assert "git checkout pyproject.toml" not in msg


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
            lambda t: (t / "scripts").chmod(0o000),
            id="unreadable-parent",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions"),
        ),
    ],
)
def test_bump_self_check_script_problem_advice(tmp_path, monkeypatch, break_check_script):
    """Every pre-run self-check branch (missing, directory, unreadable parent)
    names the check script as the problem with the same restore advice — and
    never suggests checking out the bump's own files. (An unreadable FILE is
    the run-OSError branch, pinned by test_bump_self_check_unrunnable_is_clean_error:
    a mocked runner cannot produce execve's EACCES.)"""
    make_tree(tmp_path)
    scripts_dir = tmp_path / "scripts"
    check_script = scripts_dir / "check_version_consistency.py"
    try:
        break_check_script(tmp_path)
        fake, _ = make_runner()
        with pytest.raises(SystemExit) as exc:
            run_bump(tmp_path, monkeypatch, runner=fake)
        msg = str(exc.value.code)
        assert "Restore the check script with:" in msg
        assert "git checkout scripts/check_version_consistency.py" in msg
        assert "git checkout pyproject.toml" not in msg
        assert "git checkout uv.lock" not in msg
    finally:
        # chmod-0 paths defeat pytest's rename-based cleanup — restore them.
        # Only the file shape is chmod'd: the directory shape stays 0o755
        # (a no-execute dir would defeat the cleanup again).
        scripts_dir.chmod(0o755)
        if check_script.is_file():
            check_script.chmod(0o644)


def test_bump_self_check_unrunnable_is_clean_error(tmp_path, monkeypatch):
    """An unreadable check script makes execve raise PermissionError (an
    OSError) from the self-check run — the same branch a missing interpreter
    hits; the bump must exit with the check-script advice, never traceback."""
    make_tree(tmp_path)

    def noexec_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        raise PermissionError(args[0])

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=noexec_self_check)
    msg = str(exc.value.code)
    assert "could not run the self-check" in msg
    assert "Restore the check script with:" in msg


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


def test_bump_self_check_timeout_is_clean_error(tmp_path, monkeypatch):
    """A check script that hangs (e.g. replaced by a loop) must hit the 60s
    bound as a clean timeout error, never a silent hang."""
    make_tree(tmp_path)

    def hanging_self_check(args, **kwargs):
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        assert kwargs.get("timeout") == 60, "self-check must be bounded"
        raise subprocess.TimeoutExpired(args, 60)

    with pytest.raises(SystemExit) as exc:
        run_bump(tmp_path, monkeypatch, runner=hanging_self_check)
    msg = str(exc.value.code)
    assert "timed out" in msg
    assert "Restore the check script with:" in msg
    assert "uncommitted edits" in msg  # the destructive side of git checkout is named


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xff\xfe", id="no-trailing-newline"),
        pytest.param(b"\xff\xfe\n", id="trailing-newline"),
    ],
)
def test_bump_check_script_garbage_stdout_exits_1(tmp_path, payload):
    """A broken check script emitting raw bytes must not traceback the bump
    with a UnicodeDecodeError — errors='replace' routes it to the gate's
    clean 'did not pass' message, and the echo adds no double newline. Both
    payloads run under the ASCII locale so the U+FFFD rendering is
    deterministic ('??') — that run also makes the stdout reconfigure
    load-bearing."""
    make_tree(tmp_path)
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        f"import sys\nsys.stdout.buffer.write({payload!r})\n"
    )
    proc = run_bump_in_subprocess(
        tmp_path,
        env_extra={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        require_ascii=True,
    )
    combined = proc.stdout + proc.stderr
    assert "UnicodeDecodeError" not in combined
    assert "did not pass" in combined
    assert "??\n" in proc.stdout  # echoed once, on its own line
    assert "??\n\n" not in proc.stdout  # no double newline from the echo logic


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
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text('{"name": "rtfm",')
    with pytest.raises(SystemExit, match=r'cannot read "version"'):
        run_bump(tmp_path, monkeypatch)


def test_bump_plugin_json_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text('{"name": "rtfm"}')
    with pytest.raises(SystemExit, match=r'cannot read "version"'):
        run_bump(tmp_path, monkeypatch)


def test_bump_plugin_json_version_must_be_string(tmp_path, monkeypatch):
    """pyproject version stays "0.5.1"; plugin.json's int version must be
    refused by the JSON-side str guard (if pyproject also had 5, the TOML
    guard would fire first — this fixture isolates the JSON side)."""
    make_tree(tmp_path)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": 5\n}\n'
    )
    with pytest.raises(SystemExit, match="must be a string"):
        run_bump(tmp_path, monkeypatch)


def test_bump_pyproject_as_directory_is_clean_error(tmp_path, monkeypatch):
    """The bump's read of pyproject.toml hits the same OSError class (a
    directory where the file belongs); the check script's twin is pinned
    above, this pins the bump's _read_text branch."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").mkdir()
    with pytest.raises(SystemExit, match="pyproject.toml"):
        run_bump(tmp_path, monkeypatch)


def test_bump_missing_pyproject_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    with pytest.raises(SystemExit, match="pyproject.toml not found"):
        run_bump(tmp_path, monkeypatch)


def test_bump_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    """A pyproject.toml that parses but lacks [project].version must exit
    cleanly, not KeyError-traceback."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rtfm-mcp"\n')
    with pytest.raises(SystemExit, match=r"cannot read \[project\]\.version"):
        run_bump(tmp_path, monkeypatch)


def test_bump_version_must_be_string(tmp_path, monkeypatch):
    """An unquoted `version = 5` would flow a list/int repr into the drift
    message; the reader must refuse the shape instead."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm-mcp"\nversion = 5\ndependencies = []\n'
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


def test_bump_refuses_ambiguous_version_lines(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        (tmp_path / "pyproject.toml").read_text() + '\n[tool.other]\nversion = "0.5.1"\n'
    )
    with pytest.raises(SystemExit, match="expected exactly one"):
        run_bump(tmp_path, monkeypatch)


def test_bump_second_write_refusal_leaves_the_first_file_untouched(tmp_path, monkeypatch):
    """If the plugin.json pattern can't match (say the file format shifted), the
    refusal must fire BEFORE pyproject.toml is written — otherwise a retry
    dead-ends on the drift refusal against the half-edited tree."""
    make_tree(tmp_path)
    # "version" : "0.5.1" (space before colon) — the line-anchored pattern
    # `^(\s*"version": ")` cannot match this.
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        '{\n  "name" : "rtfm",\n  "version" : "0.5.1"\n}\n'
    )
    with pytest.raises(SystemExit, match="expected exactly one"):
        run_bump(tmp_path, monkeypatch)
    assert 'version = "0.5.1"' in (tmp_path / "pyproject.toml").read_text()


def test_bump_preserves_comma_when_version_is_mid_object(tmp_path, monkeypatch):
    """The real plugin.json has "version" as a middle key followed by a comma;
    the `,?` pattern must echo the comma back or the file becomes invalid JSON."""
    make_tree(tmp_path)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": "0.5.1",\n  "mcpServers": {}\n}\n'
    )
    fake, _ = make_runner()
    assert run_bump(tmp_path, monkeypatch, runner=fake) == 0
    text = (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").read_text()
    assert '"version": "0.5.2",' in text
    assert json.loads(text)["version"] == "0.5.2"


def test_bump_writes_utf8_under_ascii_locale(tmp_path):
    """The explicit UTF-8 writes must survive an ASCII locale: pyproject and
    plugin.json carry em-dashes, and a locale-encoding write would truncate
    the file in open('w') before UnicodeEncodeError — a ValueError outside
    the OSError handler."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm-mcp"\nversion = "0.5.1"\ndescription = "test — em dash"\n'
        'dependencies = [\n    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "description": "test — em dash",\n  "version": "0.5.1"\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "print('OK: version declarations are consistent')\n"
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
    plugin_text = (
        tmp_path / "plugins/claude/.claude-plugin" / "plugin.json"
    ).read_text(encoding="utf-8")
    assert '"version": "0.5.2"' in plugin_text
    assert "test — em dash" in plugin_text  # the U+2014 glyph survived, not mangled
    plugin = json.loads(plugin_text)
    assert plugin["version"] == "0.5.2"
