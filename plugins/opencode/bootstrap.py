#!/usr/bin/env python3
"""Front bootstrap: install the pinned rtfm-mcp core and exec the server.

Each front ships this file byte-for-byte. The version comes from the front's
own manifest, so the front has one pin and cannot claim a core it will not run.
Diagnostics go to stderr; stdout belongs to the MCP protocol.
"""

from __future__ import annotations

import contextlib
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
    """Validate --front against the directory the bootstrap lives in."""
    if root_name not in {"claude", "opencode"}:
        raise SystemExit(
            f"[rtfm] FATAL: unrecognized front directory {root_name!r}; "
            "expected claude or opencode"
        )
    front = root_name
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
        if value != root_name:
            raise SystemExit(
                f"[rtfm] FATAL: --front {value!r} does not match the front "
                f"directory {root_name!r}"
            )
        front = value
    return front


def venv_server(version: str, front: str) -> list[str]:
    """Create/reuse a private venv with the pinned core, return its command."""
    vdir = data_dir(front) / "venv"
    py = _venv_python(vdir)
    stamp = vdir / ".core-version"
    if not py.exists():
        # venv.create prints ensurepip's guidance to stdout (and raises
        # SystemExit) when python3-venv is missing. stdout is the MCP stream,
        # so capture it and route it to stderr through the normal FATAL path.
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
            # venv.create writes bin/python before ensurepip runs; a partial
            # venv would make every retry take the pip branch and fail forever.
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
    if not stamp.is_file() or stamp.read_text(encoding="utf-8") != version:
        subprocess.check_call(
            [
                str(py),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                f"{CORE_PACKAGE}=={version}",
            ],
            stdout=sys.stderr,
        )
        stamp.write_text(version, encoding="utf-8")
    return [str(py), "-m", "rtfm_server"]


def resolve_command(version: str, front: str) -> list[str]:
    if shutil.which("uvx"):
        return ["uvx", "--from", f"{CORE_PACKAGE}=={version}", "rtfm-server"]
    if shutil.which("uv"):
        return ["uv", "tool", "run", "--from", f"{CORE_PACKAGE}=={version}", "rtfm-server"]
    return venv_server(version, front)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent

    try:
        front = parse_front(args, root.name)
        version = read_version(root)
        command = resolve_command(version, front)
        # execvp, not execv: uvx/uv are PATH names in the fast path.
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
