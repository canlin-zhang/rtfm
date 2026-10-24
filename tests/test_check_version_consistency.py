# tests/test_check_version_consistency.py
"""Contract tests for scripts/check_version_consistency.py."""

import json

import pytest

from tests.helpers.version_tree import make_tree, run_check, run_check_in_subprocess


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: (t / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "rtfm", "version": "0.5.0"})
        ),
        lambda t: (t / "plugins/opencode/package.json").write_text(
            json.dumps({"name": "rtfm-opencode-plugin", "version": "0.5.0"})
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
    assert run_check(tmp_path, monkeypatch) == 1


def test_check_consistent_tree_exits_0(tmp_path, monkeypatch):
    make_tree(tmp_path)
    assert run_check(tmp_path, monkeypatch) == 0


def test_check_missing_rtfm_lock_entry_exits_1(tmp_path, monkeypatch, capsys):
    make_tree(tmp_path, lock_names=("other",))
    assert run_check(tmp_path, monkeypatch) == 1
    assert "no [[package]] entry named 'rtfm-mcp'" in capsys.readouterr().err


def test_check_duplicate_rtfm_lock_entries_exit_1(tmp_path, monkeypatch, capsys):
    """Two rtfm entries — first-wins would silently pass on the first; the
    count guard must refuse."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "rtfm-mcp"\nversion = "0.5.1"\nsource = { virtual = "." }\n'
        '[[package]]\nname = "rtfm-mcp"\nversion = "9.9.9"\nsource = { virtual = "." }\n'
    )
    assert run_check(tmp_path, monkeypatch) == 1
    assert "2 [[package]] entries named 'rtfm-mcp'" in capsys.readouterr().err


def test_check_lock_missing_package_array_is_clean_error(tmp_path, monkeypatch):
    """A uv.lock that parses but has no [[package]] table must exit cleanly,
    not KeyError-traceback."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with pytest.raises(SystemExit, match="missing key 'package'"):
        run_check(tmp_path, monkeypatch)


def test_check_lock_package_wrong_shape_is_clean_error(tmp_path, monkeypatch):
    """`package = "rtfm"` parses as valid TOML; without the shape guard the
    entry lookup AttributeError-tracebacks on it. CI always runs against the
    well-formed real lock, so only a fixture can pin this."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text('package = "rtfm"\n')
    assert run_check(tmp_path, monkeypatch) == 1


def test_check_rtfm_entry_without_version_key_is_clean_error(tmp_path, monkeypatch, capsys):
    """An rtfm entry that exists but carries no version is a different failure
    than a missing entry — the message must distinguish the two."""
    make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "rtfm-mcp"\nsource = { virtual = "." }\n'
    )
    assert run_check(tmp_path, monkeypatch) == 1
    assert "has no version key" in capsys.readouterr().err


def test_check_versions_must_be_strings(tmp_path, monkeypatch):
    """All three versions as equal ints, with deps matching on both sides:
    guard-less code compares equal and reports OK — the false-pass shape this
    suite exists to catch. (An empty pyproject deps list would exit 1 via deps
    drift instead, never reaching a verdict on the versions.)"""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rtfm-mcp"\nversion = 5\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n'
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "rtfm-mcp"\nversion = 5\nsource = { virtual = "." }\n'
    )
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "rtfm",\n  "version": 5\n}\n'
    )
    assert run_check(tmp_path, monkeypatch) == 1


def test_check_pyproject_missing_version_key_is_clean_error(tmp_path, monkeypatch):
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rtfm-mcp"\n')
    with pytest.raises(SystemExit, match="missing key 'project.version'"):
        run_check(tmp_path, monkeypatch)


def test_check_non_table_project_is_clean_error(tmp_path, monkeypatch):
    """`project = 5` is valid TOML; without the isinstance guard the descent
    into it TypeErrors. The guard must exit cleanly."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text("project = 5\n")
    with pytest.raises(SystemExit, match="missing key"):
        run_check(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "name, write_bad",
    [
        pytest.param(
            "pyproject.toml",
            lambda t: (t / "pyproject.toml").write_text("this is not [ toml"),
            id="pyproject",
        ),
        pytest.param(
            "plugins/claude/.claude-plugin/plugin.json",
            lambda t: (
                t / "plugins/claude/.claude-plugin" / "plugin.json"
            ).write_text('{"name": "rtfm",'),
            id="plugin-json",
        ),
    ],
)
def test_check_malformed_config_is_clean_error(tmp_path, monkeypatch, name, write_bad):
    """Garbage in a config file must exit cleanly with 'unparseable' — both
    loader branches (TOML and JSON) exit through the same except clause, so
    one parametrized test pins them."""
    make_tree(tmp_path)
    write_bad(tmp_path)
    with pytest.raises(SystemExit, match="unparseable"):
        run_check(tmp_path, monkeypatch)


def test_check_pyproject_as_directory_is_clean_error(tmp_path, monkeypatch):
    """A directory where pyproject.toml belongs raises IsADirectoryError (an
    OSError) from read_text — the catch must exit cleanly. Directory fixtures
    work as root, so no skipif is needed."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").mkdir()
    with pytest.raises(SystemExit, match="pyproject.toml"):
        run_check(tmp_path, monkeypatch)


def test_check_missing_file_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """A missing file is a clean SystemExit with a message, never a FileNotFoundError
    traceback — pytest.raises(SystemExit) would fail the test on the latter."""
    make_tree(tmp_path)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").unlink()
    with pytest.raises(SystemExit, match="plugin.json not found"):
        run_check(tmp_path, monkeypatch)


def test_check_non_utf8_file_is_clean_error(tmp_path, monkeypatch):
    """A non-UTF-8 file raises UnicodeDecodeError — a ValueError subclass, not
    OSError — and must exit cleanly with its own message, never traceback."""
    make_tree(tmp_path)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SystemExit, match="not UTF-8"):
        run_check(tmp_path, monkeypatch)


@pytest.mark.parametrize("blank_version", ["", "   ", "\\t"])
def test_check_blank_versions_are_clean_error(tmp_path, monkeypatch, capsys, blank_version):
    """Blank versions (empty or whitespace-only) in all three files would
    pass the isinstance guard and set-equality as 'OK' — the guard must
    refuse the shape and name the offending files."""
    make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "rtfm-mcp"\nversion = "{blank_version}"\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "rtfm-mcp"\nversion = "{blank_version}"\n'
        'source = { virtual = "." }\n',
        encoding="utf-8",
    )
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        f'{{"name": "rtfm", "version": "{blank_version}"}}\n',
        encoding="utf-8",
    )
    assert run_check(tmp_path, monkeypatch) == 1
    assert "offending:" in capsys.readouterr().err  # the guard names the files


def test_check_prints_drift_under_ascii_locale(tmp_path):
    """Non-ASCII version values must not crash the check's drift listing
    under an ASCII locale — the check's stdout reconfigure (the bump has the
    same guard) must keep the report printing."""
    # The four fixture values (0.5.1 / 0.5.2 / 0.5.3 / 0.5.4) must stay mutually
    # non-prefix: a value like 0.5.10 would make '0.5.1' match its rendering
    # and silently weaken the pins below.
    make_tree(tmp_path, lock_version="0.5.2—β", opencode_version="0.5.4—β")
    # A distinct value for each front manifest: with only three distinct
    # prefixes, a swap between the two fronts' lines rendered identically and
    # passed every pin; four distinct prefixes make all permutations
    # distinguishable. (lock_version= feeds only uv.lock; pyproject keeps the
    # skeleton's 0.5.1, plugin.json is overwritten right here.)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
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
    # All four listing lines are pinned by their ASCII-safe prefixes, with a
    # DISTINCT prefix per file (0.5.1 / 0.5.2 / 0.5.3 / 0.5.4) so any
    # mis-associated value or swapped entry fails the pins. The non-ASCII tails
    # render as '??' on the reconfigured stdout — unpinned.
    assert "uv.lock (rtfm-mcp entry): 0.5.2" in proc.stdout
    assert "plugins/claude/.claude-plugin/plugin.json: 0.5.3" in proc.stdout
    assert "plugins/opencode/package.json: 0.5.4" in proc.stdout
    # The verdict is stderr and the data lines stdout — placement is part of
    # the pin, matching the suite's documented convention.
    assert "differs across files" in proc.stderr
    assert "run scripts/bump_version.py" in proc.stderr  # the actionable half
    assert proc.returncode == 1


def test_check_reads_utf8_under_ascii_locale(tmp_path):
    """The explicit UTF-8 reads must keep working under an ASCII locale: the
    repo files carry em-dashes, and locale read_text would mislabel a valid
    UTF-8 file 'not UTF-8'. The fixture puts an em-dash in plugin.json."""
    make_tree(tmp_path)
    plugin_json = (
        '{\n  "name": "rtfm",\n  "description": "test — em dash",\n  "version": "0.5.1"\n}\n'
    )
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        plugin_json, encoding="utf-8"
    )
    proc = run_check_in_subprocess(
        tmp_path,
        env_extra={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        require_ascii=True,
    )
    assert proc.returncode == 0
    assert "OK" in proc.stdout
