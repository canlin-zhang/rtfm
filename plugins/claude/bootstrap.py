#!/usr/bin/env python3
"""Front bootstrap: install the pinned rtfm-mcp core and exec the server.

Each front ships this file byte-for-byte. The version comes from the front's
own manifest, so the front has one pin and cannot claim a core it will not run.
Diagnostics go to stderr; stdout belongs to the MCP protocol.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

CORE_PACKAGE = "rtfm-mcp"


def read_version(root: Path) -> str:
    """The front's version: package.json (OpenCode) or the Claude manifest."""
    for path in (root / "package.json", root / ".claude-plugin" / "plugin.json"):
        if not path.is_file():
            continue
        try:
            version = json.loads(path.read_text(encoding="utf-8"))["version"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            raise SystemExit(f"[rtfm] FATAL: cannot read {path}: {e}") from e
        if not isinstance(version, str) or not version.strip():
            raise SystemExit(f"[rtfm] FATAL: {path} has no version")
        return version.strip()
    raise SystemExit(
        f"[rtfm] FATAL: no package.json or .claude-plugin/plugin.json under {root}"
    )


def corpus_home() -> Path:
    return Path(os.environ.get("RTFM_HOME", Path.home() / ".rtfm")).expanduser()


def data_dir(front: str) -> Path:
    """Claude gets its plugin data dir; both otherwise use the corpus home."""
    if front == "claude":
        plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
        if plugin_data:
            return Path(plugin_data).expanduser()
    return corpus_home() / "fronts" / front


def _venv_python(vdir: Path) -> Path:
    return vdir / "bin" / "python"


def parse_front(args: list[str], root_name: str) -> str:
    """Resolve the front: --front is authoritative, the directory is a fallback.

    An installed plugin lives in a versioned or renamed directory (a marketplace
    cache uses <plugin>/<version>/), so the directory name cannot be the front's
    identity. The MCP config passes --front explicitly; the fallback keeps a
    direct `python3 plugins/claude/bootstrap.py` invocation working in the repo.
    """
    front = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--front":
            if i + 1 >= len(args):
                raise SystemExit("[rtfm] FATAL: --front needs a value")
            value = args[i + 1]
            i += 2
        elif arg.startswith("--front="):
            value = arg.split("=", 1)[1]
            i += 1
        else:
            raise SystemExit(f"[rtfm] FATAL: unknown argument {arg!r}")
        if value not in {"claude", "opencode"}:
            raise SystemExit(
                f"[rtfm] FATAL: unknown front {value!r}; expected claude or opencode"
            )
        front = value
    if front is not None:
        return front
    if root_name in {"claude", "opencode"}:
        return root_name
    raise SystemExit(
        f"[rtfm] FATAL: cannot tell which front this is from directory {root_name!r}; "
        "pass --front claude or --front opencode"
    )


@contextlib.contextmanager
def _install_lock(vdir: Path):
    """One installer at a time: two sessions can start on a fresh venv together."""
    vdir.parent.mkdir(parents=True, exist_ok=True)
    with open(vdir.parent / ".install.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _create_venv(vdir: Path, uv: str | None) -> None:
    """Create the front's private venv. uv needs no system venv module."""
    if uv:
        subprocess.check_call(
            [uv, "venv", "--python", ">=3.11", str(vdir)], stdout=sys.stderr)
        return
    # venv.create prints ensurepip's guidance to stdout (and raises SystemExit)
    # when python3-venv is missing. stdout is the MCP stream, so capture it and
    # route it to stderr through the normal FATAL path.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            venv.create(vdir, with_pip=True)
    except BaseException as e:
        sys.stderr.write(captured.getvalue())
        detail = getattr(e, "output", None) or getattr(e, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        if detail:
            sys.stderr.write(detail if detail.endswith("\n") else detail + "\n")
        # venv.create writes bin/python before ensurepip runs; a partial venv
        # would make every retry take the pip branch and fail forever.
        shutil.rmtree(vdir, ignore_errors=True)
        if isinstance(e, SystemExit):
            raise RuntimeError(
                "cannot create the venv: python3-venv may be missing "
                "(Debian/Ubuntu: apt install python3-venv); the partial "
                "venv was removed, so retry is safe"
            ) from None
        raise
    if captured.getvalue():
        sys.stderr.write(captured.getvalue())


def _install_core(py: Path, version: str, uv: str | None) -> None:
    """Install the pinned core. The first attempt uses the cached index; only a
    failure retries with a targeted refresh, so a just-published version is
    found without paying for a metadata refresh on every update."""
    if uv:
        command = [uv, "pip", "install", "--python", str(py), f"{CORE_PACKAGE}=={version}"]
    else:
        command = [str(py), "-m", "pip", "install", "--quiet",
                   "--disable-pip-version-check", f"{CORE_PACKAGE}=={version}"]
    try:
        subprocess.check_call(command, stdout=sys.stderr)
    except subprocess.CalledProcessError:
        if not uv:
            raise
        subprocess.check_call(
            [uv, "pip", "install", "--refresh-package", CORE_PACKAGE,
             "--python", str(py), f"{CORE_PACKAGE}=={version}"],
            stdout=sys.stderr,
        )


def venv_server(version: str, front: str) -> list[str]:
    """Install/reuse the pinned core in a private venv, return its command."""
    vdir = data_dir(front) / "venv"
    py = _venv_python(vdir)
    stamp = vdir / ".core-version"
    uv = find_tool("uv")
    with _install_lock(vdir):
        if not py.exists():
            if vdir.exists():           # an interrupted create: start clean
                shutil.rmtree(vdir, ignore_errors=True)
            _create_venv(vdir, uv)
        if not stamp.is_file() or stamp.read_text(encoding="utf-8") != version:
            _install_core(py, version, uv)
            stamp.write_text(version, encoding="utf-8")
    return [str(py), "-m", "rtfm_server"]


def find_tool(name: str) -> str | None:
    """Locate a tool on PATH, then in the usual user-local install dirs.

    An MCP client does not necessarily launch the server with the shell's PATH:
    a `uv` installed by the standalone installer lives in ~/.local/bin, which a
    GUI client may omit. Falling through to the venv path there fails on hosts
    without python3-venv, so the common install locations are checked too."""
    found = shutil.which(name)
    if found:
        return found
    for directory in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin",
                      Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent

    try:
        front = parse_front(args, root.name)
        version = read_version(root)
        command = venv_server(version, front)
        # execvp keeps the PATH lookup; the venv interpreter is absolute.
        os.execvp(command[0], command)
    except SystemExit:
        raise
    except Exception as e:  # loud, actionable; never a silent MCP hang
        sys.stderr.write(
            f"[rtfm] FATAL: could not start the server: {type(e).__name__}: {e}\n"
            "[rtfm] Recover: install uv (https://docs.astral.sh/uv/) or ensure "
            "python3 + pip work, then retry.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
