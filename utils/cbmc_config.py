"""Configuration and subprocess wrappers for codebase-memory-mcp integration.

The binary runs on stdio (MCP protocol) — we call it via subprocess, not HTTP.

Config resolution for the binary path:
  1. LEGACY_REVERSE_CBMC_BIN env var (user override)
  2. legacy-reverse.toml → [cbmc] binary_path (repo setting; opt-in, see below)
  3. Repo .env → LEGACY_REVERSE_CBMC_BIN (opt-in, see below)
  4. Default: ~/.local/bin/codebase-memory-mcp, then PATH

Steps 2 and 3 name an *executable* inside the repo being analyzed — a hostile
legacy repo could point them at an arbitrary binary. They are therefore honored
only when the user explicitly trusts the repo via LEGACY_REVERSE_TRUST_REPO_CONFIG=1;
otherwise they are announced and skipped. The ``[cbmc] project`` toml key is plain
data (a project name), so it is always read regardless of the trust flag.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_TOML_CBMC_RE = re.compile(r"^\[cbmc\]", re.MULTILINE)
_ENV_RE = re.compile(r"^(\w[\w\d_]*)\s*=\s*(.+)$", re.MULTILINE)


# ------------------------------------------------------------
# .env loader (lightweight, no external dependency)
# ------------------------------------------------------------

def _load_env_file(path: Path) -> bool:
    """Load KEY=VALUE pairs from a ``.env`` file, skipping comments."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    for m in _ENV_RE.finditer(text):
        key = m.group(1)
        value = m.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


# ------------------------------------------------------------
# legacy-reverse.toml loader
# ------------------------------------------------------------

def _load_cbmc_from_toml(toml_path: Path) -> dict[str, str]:
    """Read the ``[cbmc]`` section and return {key: value} pairs."""
    if not toml_path.exists():
        return {}
    try:
        raw = toml_path.read_text(encoding="utf-8")
        if "[cbmc]" not in raw:
            return {}
        parts = _TOML_CBMC_RE.split(raw)
        if len(parts) < 2:
            return {}
        section = parts[1]
        cfg: dict[str, str] = {}
        for line in section.splitlines():
            line = line.strip()
            if line.startswith("["):
                break
            if "=" in line and not line.startswith("#"):
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val:
                    cfg[key] = val
        return cfg
    except (OSError, UnicodeDecodeError):
        return {}


# ------------------------------------------------------------
# Public API — config resolution
# ------------------------------------------------------------

def load_env_file(repo_path: str | Path | None = None) -> bool:
    """Load .env from the repo root (if present)."""
    if repo_path:
        return _load_env_file(Path(repo_path) / ".env")
    return _load_env_file(Path(".env"))


def _default_binary_path() -> str:
    """Try to locate codebase-memory-mcp in standard locations.

    Returns the resolved path if found, otherwise "codebase-memory-mcp" (for PATH lookup).
    """
    # 1. Check standard user-local install path: ~/.local/bin/codebase-memory-mcp
    default_path = Path.home() / ".local" / "bin" / "codebase-memory-mcp"
    if default_path.exists():
        return str(default_path)

    # 2. Fall back to PATH lookup
    return "codebase-memory-mcp"


def _repo_config_trusted() -> bool:
    """The repo's own toml/.env may name an executable to run — that is only safe
    when the user explicitly vouches for the repo being analyzed."""
    return os.environ.get("LEGACY_REVERSE_TRUST_REPO_CONFIG", "") == "1"


def resolve_cbmc_config(
    repo_path: str | Path | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Resolve CBMC binary path and config.

    Resolution order:
      1. LEGACY_REVERSE_CBMC_BIN env var (user override)
      2. legacy-reverse.toml → [cbmc] binary_path (only with LEGACY_REVERSE_TRUST_REPO_CONFIG=1)
      3. Repo .env → LEGACY_REVERSE_CBMC_BIN (only with LEGACY_REVERSE_TRUST_REPO_CONFIG=1)
      4. Standard user-local path ~/.local/bin/codebase-memory-mcp, then PATH lookup

    Returns (binary_path_or_None, config_dict). None means explicitly disabled.
    The config dict is the toml ``[cbmc]`` section and is returned from every
    branch — ``project`` pinning must survive whichever way the binary resolves.
    """
    repo = Path(repo_path) if repo_path else Path.cwd()
    toml_cfg = _load_cbmc_from_toml(repo / "legacy-reverse.toml")

    # 1. User env var (highest priority)
    env_val = os.environ.get("LEGACY_REVERSE_CBMC_BIN")
    if env_val is not None:
        return (env_val or None), toml_cfg

    # 2. Repo toml names an executable — honored only for explicitly trusted repos
    if "binary_path" in toml_cfg:
        if _repo_config_trusted():
            bp = toml_cfg["binary_path"]
            return (bp or None), toml_cfg
        print("CBMC: [cbmc] binary_path in legacy-reverse.toml ignored (repo-supplied "
              "executable); set LEGACY_REVERSE_TRUST_REPO_CONFIG=1 to allow it")

    # 3. Repo .env — same trust boundary as the toml
    if _repo_config_trusted():
        load_env_file(repo)
        env_val = os.environ.get("LEGACY_REVERSE_CBMC_BIN")
        if env_val:
            return env_val, toml_cfg

    # 4. Default — standard path or PATH
    return _default_binary_path(), toml_cfg


# ------------------------------------------------------------
# Subprocess wrappers for codebase-memory-mcp CLI
# ------------------------------------------------------------

def cbmc_available(binary: str | None = None) -> bool:
    """Check if the codebase-memory-mcp binary is available."""
    bin_path = binary or resolve_cbmc_config()[0]
    if not bin_path:
        return False
    return bool(shutil.which(bin_path) or os.path.isfile(bin_path))


def cbmc_call(
    tool: str,
    args: str | dict | None = None,
    binary: str | None = None,
    timeout: float = 300.0,
    repo_path: str | None = None,
) -> tuple[dict | None, dict]:
    """Call a codebase-memory-mcp CLI tool.

    Returns (result_dict_or_None, info_dict).
    The info dict always has 'tool', 'binary', 'exit_code' and possibly 'error'.
    """
    bin_path = binary or resolve_cbmc_config(repo_path)[0]
    if not bin_path:
        return None, {"tool": tool, "error": "CBMC disabled"}

    resolved = shutil.which(bin_path) or bin_path
    if not os.path.isfile(resolved):
        return None, {"tool": tool, "error": f"binary not found: {resolved}"}

    # Build argv
    argv = [resolved, "cli", tool]
    if isinstance(args, dict):
        # Convert dict to CLI flags: --key value; a False bool means "flag absent",
        # not "--key ''" (an empty positional the binary would misparse)
        for key, value in args.items():
            if isinstance(value, bool):
                if value:
                    argv.extend([f"--{key}", "true"])
                continue
            argv.extend([f"--{key}", str(value)])
    elif args:
        argv.append(str(args))

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, {"tool": tool, "binary": bin_path, "exit_code": -1, "error": f"timeout after {timeout:.0f}s"}
    except OSError as exc:
        return None, {"tool": tool, "binary": bin_path, "error": str(exc)}

    info = {"tool": tool, "binary": bin_path, "exit_code": proc.returncode}

    if proc.returncode != 0:
        info["error"] = proc.stderr.strip()[-500:]
        return None, info

    # Parse JSON output: decode from the first '{' onwards, so warning lines
    # before the payload and pretty-printed (multi-line) JSON both survive;
    # raw_decode also tolerates trailing noise after the object.
    start = proc.stdout.find("{")
    if start == -1:
        return None, {**info, "error": "no JSON output"}

    try:
        result, _ = json.JSONDecoder().raw_decode(proc.stdout[start:])
        return result, info
    except json.JSONDecodeError:
        return None, {**info, "error": f"bad JSON: {proc.stdout[start:start + 200]}"}


# ------------------------------------------------------------
# Convenience wrappers for common tools
# ------------------------------------------------------------

def cbmc_list_projects(binary: str | None = None) -> tuple[list[dict], dict]:
    """List indexed projects."""
    result, info = cbmc_call("list_projects", "{}", binary=binary)
    projects = result.get("projects", []) if result else []
    return projects, info


def cbmc_index_repository(
    repo_path: str,
    mode: str = "full",
    name: str | None = None,
    binary: str | None = None,
) -> tuple[dict | None, dict]:
    """Index a repository into codebase-memory."""
    payload: dict[str, Any] = {"repo_path": repo_path, "mode": mode}
    if name:
        payload["name"] = name
    return cbmc_call("index_repository", payload, binary=binary, timeout=600)


def cbmc_search_graph(
    query: str,
    project: str | None = None,
    limit: int = 20,
    binary: str | None = None,
) -> tuple[dict | None, dict]:
    """Semantic search via vector cosine."""
    payload: dict[str, Any] = {"query": query, "limit": limit}
    if project:
        payload["project"] = project
    return cbmc_call("search_graph", payload, binary=binary, timeout=30)


def cbmc_get_architecture(
    project: str,
    aspects: list[str] | None = None,
    binary: str | None = None,
) -> tuple[dict | None, dict]:
    """Get architecture overview (clusters, layers, etc.)."""
    payload: dict[str, Any] = {"project": project}
    if aspects:
        payload["aspects"] = aspects
    return cbmc_call("get_architecture", payload, binary=binary, timeout=60)


def cbmc_get_code_snippet(
    qualified_name: str,
    project: str | None = None,
    include_neighbors: bool = True,
    binary: str | None = None,
    timeout: float = 30.0,
) -> tuple[dict | None, dict]:
    """Retrieve code snippet for a symbol from the knowledge graph."""
    payload: dict[str, Any] = {"qualified_name": qualified_name}
    if project:
        payload["project"] = project
    if include_neighbors:
        payload["include_neighbors"] = True
    return cbmc_call("get_code_snippet", payload, binary=binary, timeout=timeout)


def cbmc_detect_changes(
    project: str,
    base_branch: str = "main",
    since: str | None = None,
    binary: str | None = None,
) -> tuple[dict | None, dict]:
    """Detect code changes and their impact."""
    payload: dict[str, Any] = {"project": project, "base_branch": base_branch}
    if since:
        payload["since"] = since
    return cbmc_call("detect_changes", payload, binary=binary, timeout=120)
