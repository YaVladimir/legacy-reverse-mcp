"""gigacode-cli harness: run the GigaCode ``architecture-generator`` skill and
import its flat JSON into our index.

GigaCode CLI is a Gemini-CLI fork, so it runs **headless**: ``gigacode -p "<prompt>"``
prints to stdout (the skill may instead write a JSON file — both are supported).
This module shells out to it (argv list, never ``shell=True``), then hands the
resulting flat JSON to :func:`analysis.flat_arch.import_flat`.

Everything is environment-configurable, because the exact skill trigger and output
location are only known on the work machine:

    LEGACY_REVERSE_GIGACODE_CMD      default: gigacode
    LEGACY_REVERSE_GIGACODE_ARGS     default: -p          (space-separated flags before the prompt)
    LEGACY_REVERSE_GIGACODE_PROMPT   default: a request to run architecture-generator and print JSON
    LEGACY_REVERSE_GIGACODE_OUTPUT   default: stdout      (or a path to the JSON the skill writes)
    LEGACY_REVERSE_GIGACODE_TIMEOUT  default: 900         (seconds)
    LEGACY_REVERSE_GIGACODE_CWD      default: the repo    (so the skill sees the project)

If gigacode is not installed, the manual path still works: run the skill yourself,
then ``legacy-reverse import-arch --in <file>``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from analysis.flat_arch import import_flat

_DEFAULT_PROMPT = (
    "Запусти скилл architecture-generator для проекта в текущем каталоге и верни "
    "его результат — плоский JSON архитектуры (project_architecture_flat) — в stdout. "
    "Выведи ТОЛЬКО JSON, без markdown и пояснений."
)


@dataclass
class HarnessConfig:
    cmd: str = "gigacode"
    args: list[str] = field(default_factory=lambda: ["-p"])
    prompt: str = _DEFAULT_PROMPT
    output: str = "stdout"          # "stdout" or a path to a JSON file the skill writes
    timeout: float = 900.0
    cwd: str | None = None

    @classmethod
    def from_env(cls, repo_path: str | None = None) -> "HarnessConfig":
        raw_args = os.environ.get("LEGACY_REVERSE_GIGACODE_ARGS")
        args = raw_args.split() if raw_args else ["-p"]
        raw_timeout = os.environ.get("LEGACY_REVERSE_GIGACODE_TIMEOUT")
        try:
            timeout = float(raw_timeout) if raw_timeout else 900.0
        except ValueError:
            timeout = 900.0
        return cls(
            cmd=os.environ.get("LEGACY_REVERSE_GIGACODE_CMD") or "gigacode",
            args=args,
            prompt=os.environ.get("LEGACY_REVERSE_GIGACODE_PROMPT") or _DEFAULT_PROMPT,
            output=os.environ.get("LEGACY_REVERSE_GIGACODE_OUTPUT") or "stdout",
            timeout=timeout,
            cwd=os.environ.get("LEGACY_REVERSE_GIGACODE_CWD") or repo_path,
        )


def gigacode_available(cmd: str = "gigacode") -> bool:
    return bool(
        shutil.which(cmd)
        or os.environ.get("GIGACODE")
        or os.environ.get("GIGACODE_CLI")
    )


def _extract_json(text: str) -> dict | None:
    """Tolerant: parse the first balanced {...} object found in ``text``."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _build_argv(cfg: HarnessConfig) -> tuple[list[str] | None, str | None]:
    exe = shutil.which(cfg.cmd) or cfg.cmd
    if shutil.which(cfg.cmd) is None and not (
        os.environ.get("GIGACODE") or os.environ.get("GIGACODE_CLI")
    ):
        return None, f"'{cfg.cmd}' not found on PATH"
    argv = [exe, *cfg.args, cfg.prompt]
    # Windows: a .cmd/.bat shim cannot be launched directly by CreateProcess.
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    return argv, None


def run_gigacode(repo_path: str, cfg: HarnessConfig | None = None) -> tuple[dict | None, dict]:
    """Run the configured gigacode command and return (flat_json_or_None, info)."""
    cfg = cfg or HarnessConfig.from_env(repo_path)
    info: dict = {"cmd": cfg.cmd, "output": cfg.output}
    argv, err = _build_argv(cfg)
    if argv is None:
        info["error"] = err
        info["hint"] = (
            "Install/login gigacode, or run the architecture-generator skill manually "
            "and load its output with: legacy-reverse import-arch --in <file>"
        )
        return None, info

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg.timeout,
            cwd=cfg.cwd or repo_path,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        info["error"] = f"gigacode timed out after {cfg.timeout:.0f}s"
        return None, info
    except OSError as exc:
        info["error"] = f"failed to run gigacode: {exc}"
        return None, info

    info["returncode"] = proc.returncode
    if cfg.output and cfg.output != "stdout":
        out_path = Path(cfg.output)
        if not out_path.is_absolute():
            out_path = Path(cfg.cwd or repo_path) / out_path
        try:
            data = _extract_json(out_path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            info["error"] = f"could not read gigacode output file {out_path}: {exc}"
            return None, info
    else:
        data = _extract_json(proc.stdout or "")

    if data is None:
        info["error"] = "gigacode produced no parseable flat JSON"
        info["stderr_tail"] = (proc.stderr or "").strip()[-400:]
        info["stdout_tail"] = (proc.stdout or "").strip()[-400:]
    return data, info


def generate_architecture(conn: sqlite3.Connection, repo_path: str, cfg: HarnessConfig | None = None) -> dict:
    """Run gigacode's architecture-generator and import the result. Returns a stats
    dict; on failure returns ``{"status": "error", ...}`` with a hint (no exception)."""
    data, info = run_gigacode(repo_path, cfg)
    if data is None:
        return {"status": "error", "source": "gigacode", **info}
    import_stats = import_flat(conn, repo_path, data, source="gigacode")
    return {"status": "imported", "source": "gigacode", **info, **import_stats}
