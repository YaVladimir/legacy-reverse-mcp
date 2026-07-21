"""Subprocess execution that survives misbehaving process TREES.

``subprocess.run(timeout=...)`` kills only the direct child. Every external
tool here may be a wrapper (cmd /c shim, node launcher, CLI that spawns a
worker): on timeout the grandchild keeps the inherited stdout/stderr pipe
handles open, the internal ``communicate()`` retry blocks forever, and the
calling worker thread hangs despite the timeout — while the orphan keeps
burning tokens. This module kills the whole tree (taskkill /T on Windows, a
new session + killpg on POSIX) and always returns, never raises.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class ProcResult:
    returncode: int | None   # None when the process failed to run or timed out
    stdout: str
    stderr: str
    error: str | None        # human-readable launch/timeout error, None on success


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_tree_captured(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    input_text: str | None = None,
    env: dict | None = None,
) -> ProcResult:
    """Like ``subprocess.run(capture_output=True, text=True)`` but the whole
    process tree dies on timeout. ``input_text`` is piped to stdin when given."""
    popen_kwargs: dict = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # own process group for killpg
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            **popen_kwargs,
        )
    except OSError as exc:
        return ProcResult(None, "", "", str(exc))

    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return ProcResult(proc.returncode, stdout or "", stderr or "", None)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except (subprocess.SubprocessError, OSError, ValueError):
            stdout, stderr = "", ""
        return ProcResult(None, stdout or "", stderr or "", f"timed out after {timeout:.0f}s")
