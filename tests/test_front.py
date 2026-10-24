# tests/test_front.py
"""Contract tests for the harness fronts and their shared bootstrap.

The bootstrap is a script in each front, not an installed module, so the tests
load it with SourceFileLoader. Both fronts copy the same file byte-for-byte;
one test enforces that so the two install paths cannot drift.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bootstrap = _load("front_bootstrap", ROOT / "plugins/claude/bootstrap.py")


def _pyproject_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_read_version_prefers_the_opencode_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"version": "2.0.0"}')
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"version": "1.2.3"}')
    assert bootstrap.read_version(tmp_path) == "2.0.0"


def test_read_version_falls_back_to_claude_manifest(tmp_path):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"version": "1.2.3"}')
    assert bootstrap.read_version(tmp_path) == "1.2.3"


def test_data_dir_prefers_claude_plugin_data(monkeypatch, tmp_path):
    monkeypatch.setenv("RTFM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert bootstrap.data_dir("claude") == tmp_path / "data"
    assert bootstrap.data_dir("opencode") == tmp_path / "home" / "fronts" / "opencode"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    assert bootstrap.data_dir("claude") == tmp_path / "home" / "fronts" / "claude"


def test_resolve_command_prefers_uvx(monkeypatch):
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: "/bin/uvx" if name == "uvx" else None
    )
    assert bootstrap.resolve_command("0.8.0", "claude") == [
        "/bin/uvx", "--from", "rtfm-mcp==0.8.0", "rtfm-server"
    ]


def test_resolve_command_falls_back_to_uv(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None
    )
    assert bootstrap.resolve_command("0.8.0", "claude") == [
        "/bin/uv", "tool", "run", "--from", "rtfm-mcp==0.8.0", "rtfm-server"
    ]


def test_resolve_command_finds_uvx_in_a_user_local_install(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    uvx = tmp_path / ".local" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.write_text("#!/bin/sh\n")
    uvx.chmod(0o755)
    assert bootstrap.resolve_command("0.8.0", "claude")[0] == str(uvx)


def test_resolve_command_uses_venv_when_no_uv(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        bootstrap,
        "venv_server",
        lambda version, front: ["/v/bin/python", "-m", "rtfm_server"],
    )
    assert bootstrap.resolve_command("0.8.0", "claude") == [
        "/v/bin/python", "-m", "rtfm_server"
    ]


def test_main_reports_fatal_to_stderr(monkeypatch, capsys):
    def boom(root):
        raise RuntimeError("boom")

    monkeypatch.setattr(bootstrap, "read_version", boom)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--front", "claude"])
    assert exc.value.code == 1
    assert "FATAL" in capsys.readouterr().err


def test_venv_install_keeps_stdout_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)

    def fake_create(path, with_pip):
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "bin" / "python").write_text("")

    monkeypatch.setattr(bootstrap.venv, "create", fake_create)
    calls = []
    monkeypatch.setattr(
        bootstrap.subprocess, "check_call", lambda cmd, **kwargs: calls.append(kwargs)
    )

    command = bootstrap.venv_server("0.8.0", "claude")

    assert command == [str(tmp_path / "venv" / "bin" / "python"), "-m", "rtfm_server"]
    assert calls and calls[0]["stdout"] is sys.stderr


def test_venv_creation_failure_keeps_stdout_clean(monkeypatch, tmp_path, capsys):
    """ensurepip's guidance must not reach the MCP stdout stream."""
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    def broken_create(path, with_pip):
        print("ensurepip is not available", file=sys.stdout)
        raise SystemExit(1)

    monkeypatch.setattr(bootstrap.venv, "create", broken_create)

    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--front", "claude"])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ensurepip is not available" in captured.err
    assert "FATAL" in captured.err
    assert not (tmp_path / "venv" / "bin" / "python").exists()


def test_venv_creation_failure_removes_partial_venv_and_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)
    calls = []

    def broken_create(path, with_pip):
        calls.append(path)
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "bin" / "python").write_text("")
        raise SystemExit(1)

    monkeypatch.setattr(bootstrap.venv, "create", broken_create)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="python3-venv"):
            bootstrap.venv_server("0.8.0", "claude")

    assert len(calls) == 2
    assert not (tmp_path / "venv" / "bin" / "python").exists()


def test_venv_creation_failure_prints_child_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    def broken_create(path, with_pip):
        raise subprocess.CalledProcessError(1, ["python"], output=b"ensurepip exploded\n")

    monkeypatch.setattr(bootstrap.venv, "create", broken_create)

    with pytest.raises(SystemExit):
        bootstrap.main(["--front", "claude"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ensurepip exploded" in captured.err


def _make_venv(tmp_path, version):
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").write_text("")
    (tmp_path / "venv" / ".core-version").write_text(version)


def test_venv_stamp_skips_matching_install(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)
    _make_venv(tmp_path, "0.8.0")
    calls = []
    monkeypatch.setattr(
        bootstrap.subprocess, "check_call", lambda cmd, **kwargs: calls.append(cmd)
    )

    command = bootstrap.venv_server("0.8.0", "claude")

    assert command == [str(tmp_path / "venv" / "bin" / "python"), "-m", "rtfm_server"]
    assert calls == []


def test_venv_stamp_reinstalls_on_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)
    _make_venv(tmp_path, "0.7.0")
    calls = []
    monkeypatch.setattr(
        bootstrap.subprocess, "check_call", lambda cmd, **kwargs: calls.append(cmd)
    )

    bootstrap.venv_server("0.8.0", "claude")

    assert calls and "rtfm-mcp==0.8.0" in calls[0]
    assert (tmp_path / "venv" / ".core-version").read_text() == "0.8.0"


def test_parse_front_accepts_both_forms():
    assert bootstrap.parse_front(["--front", "claude"], "claude") == "claude"
    assert bootstrap.parse_front(["--front=opencode"], "opencode") == "opencode"
    assert bootstrap.parse_front([], "claude") == "claude"


def test_parse_front_accepts_the_flag_from_a_versioned_install_dir():
    # A marketplace install lives in <plugin>/<version>/, so the directory name
    # cannot be the front's identity. The flag is.
    assert bootstrap.parse_front(["--front", "claude"], "0.9.0") == "claude"
    assert bootstrap.parse_front(["--front=opencode"], "0.9.0") == "opencode"


def test_parse_front_without_a_flag_uses_the_repo_directory_name():
    assert bootstrap.parse_front([], "claude") == "claude"


def test_parse_front_without_a_flag_rejects_an_installed_dir_name():
    with pytest.raises(SystemExit, match="pass --front"):
        bootstrap.parse_front([], "0.9.0")


def test_parse_front_rejects_unknown_values():
    with pytest.raises(SystemExit, match="unknown front"):
        bootstrap.parse_front(["--front", "claud"], "claude")
    with pytest.raises(SystemExit, match="needs a value"):
        bootstrap.parse_front(["--front"], "claude")


def test_main_starts_from_a_versioned_install_directory(monkeypatch, tmp_path):
    """The exact marketplace-cache layout: the bootstrap's folder is the version."""
    import shutil

    install = tmp_path / "0.9.0"
    install.mkdir()
    shutil.copy(ROOT / "plugins" / "claude" / "bootstrap.py", install / "bootstrap.py")
    module = _load("front_bootstrap_installed", install / "bootstrap.py")
    monkeypatch.setattr(module, "read_version", lambda root: "0.9.0")
    monkeypatch.setattr(module, "resolve_command", lambda version, front: ["/bin/rtfm-server"])
    calls = []
    monkeypatch.setattr(module.os, "execvp", lambda file, args: calls.append(file))

    module.main(["--front", "claude"])

    assert calls == ["/bin/rtfm-server"]


def test_main_execs_the_resolved_command(monkeypatch):
    monkeypatch.setattr(bootstrap, "read_version", lambda root: "0.8.0")
    monkeypatch.setattr(
        bootstrap, "resolve_command", lambda version, front: ["/bin/rtfm-server"]
    )
    calls = []
    monkeypatch.setattr(
        bootstrap.os, "execvp", lambda file, args: calls.append((file, args))
    )

    bootstrap.main(["--front", "claude"])

    assert calls == [("/bin/rtfm-server", ["/bin/rtfm-server"])]


def test_fronts_do_not_contain_the_core():
    for front in ("claude", "opencode"):
        assert not (ROOT / "plugins" / front / "rtfm_server.py").exists()
        assert not (ROOT / "plugins" / front / "rtfm").exists()


def test_no_cross_discovered_skill_dirs():
    assert not (ROOT / ".claude" / "skills").exists()
    assert not (ROOT / ".agents" / "skills").exists()


def test_claude_manifest_has_no_inline_mcp_servers():
    manifest = json.loads(
        (ROOT / "plugins/claude/.claude-plugin/plugin.json").read_text()
    )
    assert "mcpServers" not in manifest


def test_front_skill_blocks_are_well_formed():
    for front in ("claude", "opencode"):
        text = (ROOT / "plugins" / front / "skills/read-the-manual/SKILL.md").read_text()
        assert text.count("<!-- front-specific:start -->") == 1
        assert text.count("<!-- front-specific:end -->") == 1
        assert text.index("<!-- front-specific:start -->") < text.index(
            "<!-- front-specific:end -->"
        )


def test_opencode_index_parses_skill_before_registering_mcp():
    text = (ROOT / "plugins/opencode/index.ts").read_text()
    assert text.index("readFile(skillPath") < text.index("ctx.mcp.transform")
    assert "throw new Error" in text
    assert 'const SERVER_NAME = "rtfm"' in text
    assert "editor.set(SERVER_NAME" in text


def test_front_bootstraps_are_byte_identical():
    assert (ROOT / "plugins/claude/bootstrap.py").read_bytes() == (
        ROOT / "plugins/opencode/bootstrap.py"
    ).read_bytes()


def test_opencode_package_version_matches_pyproject():
    pkg = json.loads((ROOT / "plugins/opencode/package.json").read_text())
    assert pkg["version"] == _pyproject_version()


def test_opencode_package_depends_on_the_plugin_api():
    pkg = json.loads((ROOT / "plugins/opencode/package.json").read_text())
    assert "@opencode/plugin" in pkg["dependencies"]


def test_opencode_index_registers_mcp_and_skill():
    text = (ROOT / "plugins/opencode/index.ts").read_text()
    assert "ctx.mcp.transform" in text and "ctx.skill.transform" in text
    assert '"--front", "opencode"' in text


def test_opencode_package_lists_runtime_files():
    pkg = json.loads((ROOT / "plugins/opencode/package.json").read_text())
    assert set(pkg["files"]) == {"index.ts", "bootstrap.py", "skills/"}


def test_skills_share_the_same_prefix():
    marker = "<!-- front-specific:start -->"
    shared = (ROOT / "skills/read-the-manual/SKILL.md").read_text().rstrip()
    claude = (ROOT / "plugins/claude/skills/read-the-manual/SKILL.md").read_text()
    opencode = (ROOT / "plugins/opencode/skills/read-the-manual/SKILL.md").read_text()
    assert claude.split(marker)[0].rstrip() == shared
    assert opencode.split(marker)[0].rstrip() == shared


def test_opencode_skill_uses_opencode_tool_names():
    text = (ROOT / "plugins/opencode/skills/read-the-manual/SKILL.md").read_text()
    assert "@read-the-manual" in text
    assert "rtfm_search" in text and "rtfm_read" in text
    assert "mcp__plugin_rtfm" not in text


def test_agents_md_describes_both_fronts_and_stays_neutral():
    text = (ROOT / "AGENTS.md").read_text()
    assert "plugins/claude/" in text and "plugins/opencode/" in text
    for needle in ("~/.config/opencode", ".opencode/", "mcp__plugin_", "CLAUDE_PLUGIN_ROOT"):
        assert needle not in text


def test_instruction_files_are_split_by_discovery_surface():
    claude = (ROOT / "CLAUDE.md").read_text()
    assert "@AGENTS.md" in claude
    cfg = json.loads((ROOT / "opencode.json").read_text())
    assert cfg["instructions"]
    for rel in cfg["instructions"]:
        assert (ROOT / rel).exists()
