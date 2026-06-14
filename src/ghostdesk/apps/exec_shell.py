# Copyright (c) 2026 Yoann Vanitou — FSL-1.1-ALv2
"""Apps exec_shell tool — run a shell command and capture its output."""

import asyncio
import shlex
from pathlib import Path

from mcp.server.fastmcp import Context

_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT_BYTES = 512 * 1024  # 512 KB — prevent context flooding


async def exec_shell(
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
    workdir: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Run a shell command synchronously and return its output.

    Executes any command available on the virtual desktop's PATH — CLI tools,
    scripts, one-liners. Use this instead of ``app_launch`` when you want
    captured output rather than a detached GUI window.

    The command is parsed with ``shlex.split`` (no shell expansion, no
    injection). If you need shell features (pipes, redirects, glob), wrap
    explicitly: ``exec_shell("bash -c 'ls /tmp | grep foo'")``.

    Args:
        command:  Command line to execute (e.g. ``"ls -la /tmp"``).
        timeout:  Seconds before the process is killed (default 30).
                  Increase for long-running conversions or builds.
        workdir:  Working directory (default: GhostDesk's CWD).

    Returns a dict with:
    - returncode: exit status (0 = success).
    - stdout: captured standard output (truncated to 512 KB).
    - stderr: captured standard error (truncated to 512 KB).
    - timed_out: True if the process was killed due to timeout.
    - command: the command as received.

    On parsing or launch failure, returns a dict with a single ``error`` key.
    """
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return {"error": f"Invalid command syntax: {e}"}

    if not parts:
        return {"error": "No command provided"}

    cwd = Path(workdir) if workdir else None

    if ctx is not None:
        await ctx.info(f"exec_shell: {command!r}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError:
        return {"error": f"Command not found: {parts[0]}"}
    except PermissionError:
        return {"error": f"Permission denied: {parts[0]}"}
    except NotADirectoryError:
        return {"error": f"Not a directory: {workdir}"}

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        stdout_bytes, stderr_bytes = await proc.communicate()

    return {
        "returncode": proc.returncode,
        "stdout": stdout_bytes[:_MAX_OUTPUT_BYTES].decode(errors="replace"),
        "stderr": stderr_bytes[:_MAX_OUTPUT_BYTES].decode(errors="replace"),
        "timed_out": timed_out,
        "command": command,
    }
