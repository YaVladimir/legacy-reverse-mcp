"""Static indexing of Spring externalized configuration.

Finds ``application*.{yml,yaml,properties}`` and ``bootstrap*.*`` files, flattens
nested YAML into dotted keys (Spring's relaxed binding form), derives the profile
from the filename (``application-<profile>.yml``) and flags secret-bearing keys.

No application context is started and no placeholders are resolved: this is a
pure read of the files on disk, so values like ``${DB_PASSWORD}`` are recorded
verbatim. Outward-facing consumers (report, MCP) mask secret values; the index
itself stays faithful to the source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import yaml

from index import repository as repo
from scanner.repo_scanner import prune_dirnames

_CONFIG_STEMS = ("application", "bootstrap")
_YAML_SUFFIXES = (".yml", ".yaml")
_PROPS_SUFFIX = ".properties"

# substrings that, when present in a (lowercased) key, mark its value secret
_SECRET_MARKERS = (
    "password", "passwd", "secret", "token", "credential",
    "private-key", "privatekey", "api-key", "apikey", "access-key", "accesskey",
)


@dataclass
class ConfigProperty:
    key: str
    value: str | None
    is_secret: bool


@dataclass
class ConfigFile:
    path: str                       # repo-relative, forward slashes
    kind: str                       # application-yaml | bootstrap-properties | ...
    profile: str | None
    properties: list[ConfigProperty] = dc_field(default_factory=list)


@dataclass
class ConfigStats:
    config_files: int = 0
    config_properties: int = 0
    secrets: int = 0
    profiles: set[str] = dc_field(default_factory=set)


# ------------------------------------------------------------
# file discovery
# ------------------------------------------------------------

def _is_config_name(name: str) -> bool:
    low = name.lower()
    if not low.startswith(_CONFIG_STEMS):
        return False
    return low.endswith(_YAML_SUFFIXES) or low.endswith(_PROPS_SUFFIX)


def _walk_config_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        prune_dirnames(Path(dirpath), dirnames)
        for name in filenames:
            if _is_config_name(name):
                yield Path(dirpath) / name


def _kind(name: str) -> str:
    low = name.lower()
    base = "bootstrap" if low.startswith("bootstrap") else "application"
    fmt = "properties" if low.endswith(_PROPS_SUFFIX) else "yaml"
    return f"{base}-{fmt}"


def _profile(name: str) -> str | None:
    """``application-dev.yml`` -> ``dev``; ``application.yml`` -> None."""
    stem = name
    for suffix in (*_YAML_SUFFIXES, _PROPS_SUFFIX):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.split("-", 1)[1] if "-" in stem else None


def _is_secret(key: str) -> bool:
    low = key.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


# ------------------------------------------------------------
# parsing
# ------------------------------------------------------------

def _stringify(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten(obj, prefix: str = "") -> list[tuple[str, str | None]]:
    """Flatten nested YAML into Spring's dotted/indexed key form."""
    out: list[tuple[str, str | None]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.extend(_flatten(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_flatten(v, f"{prefix}[{i}]"))
    else:
        out.append((prefix, None if obj is None else _stringify(obj)))
    return out


def _parse_yaml(text: str) -> list[tuple[str, str | None]]:
    pairs: list[tuple[str, str | None]] = []
    # multi-document files (profile-specific docs separated by ---) are common
    for doc in yaml.safe_load_all(text):
        if doc is None:
            continue
        pairs.extend(_flatten(doc))
    return [(k, v) for k, v in pairs if k]


def _unescape_key(key: str) -> str:
    return key.replace("\\:", ":").replace("\\=", "=").replace("\\ ", " ")


def _parse_properties(text: str) -> list[tuple[str, str | None]]:
    pairs: list[tuple[str, str | None]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip()[0] in "#!":
            continue
        # logical-line continuation: a line ending in an odd run of backslashes
        while line.endswith("\\") and (len(line) - len(line.rstrip("\\"))) % 2 == 1 and i < len(lines):
            line = line[:-1] + lines[i].lstrip()
            i += 1
        sep = _find_separator(line)
        if sep is None:
            pairs.append((_unescape_key(line.strip()), None))
            continue
        key = _unescape_key(line[:sep].strip())
        value = line[sep + 1:].strip()
        pairs.append((key, value or None))
    return [(k, v) for k, v in pairs if k]


def _find_separator(line: str) -> int | None:
    """Index of the first unescaped ``=`` or ``:`` (Java .properties separators)."""
    for idx, ch in enumerate(line):
        if ch in "=:" and (idx == 0 or line[idx - 1] != "\\"):
            return idx
    return None


def parse_config_file(path: Path) -> ConfigFile | None:
    """Parse one config file into a ConfigFile, or None if unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    name = path.name
    try:
        if name.lower().endswith(_PROPS_SUFFIX):
            pairs = _parse_properties(text)
        else:
            pairs = _parse_yaml(text)
    except yaml.YAMLError:
        pairs = []

    props = [ConfigProperty(key=k, value=v, is_secret=_is_secret(k)) for k, v in pairs]
    rel = path.name  # replaced with repo-relative path by the caller
    return ConfigFile(path=rel, kind=_kind(name), profile=_profile(name), properties=props)


def scan_config_files(repo_path: str) -> list[ConfigFile]:
    root = Path(repo_path).resolve()
    out: list[ConfigFile] = []
    for path in _walk_config_files(root):
        cfg = parse_config_file(path)
        if cfg is None:
            continue
        cfg.path = str(path.relative_to(root)).replace("\\", "/")
        out.append(cfg)
    return out


# ------------------------------------------------------------
# module association + persistence
# ------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").rstrip("/")


def _resolve_module_id(rel_path: str, modules: list) -> int | None:
    """Longest module-path prefix that contains the config file; root as fallback."""
    rel = _norm(rel_path)
    best_id: int | None = None
    best_len = -1
    root_id: int | None = None
    for m in modules:
        mpath = _norm(m["path"])
        if mpath in ("", "."):
            root_id = m["id"]
            continue
        if (rel == mpath or rel.startswith(mpath + "/")) and len(mpath) > best_len:
            best_id, best_len = m["id"], len(mpath)
    return best_id if best_id is not None else root_id


def index_config(conn, repo_path: str) -> ConfigStats:
    """Discover, parse and persist Spring config files. Returns counters."""
    repo.clear_config(conn, commit=False)
    stats = ConfigStats()

    modules = repo.list_modules(conn)
    for cfg in scan_config_files(repo_path):
        module_id = _resolve_module_id(cfg.path, modules)
        config_file_id = repo.insert_config_file(
            conn, file_path=cfg.path, kind=cfg.kind, module_id=module_id,
            profile=cfg.profile, commit=False,
        )
        for prop in cfg.properties:
            repo.insert_config_property(
                conn, config_file_id=config_file_id, key=prop.key,
                value=prop.value, is_secret=prop.is_secret, commit=False,
            )
            stats.config_properties += 1
            if prop.is_secret:
                stats.secrets += 1
        stats.config_files += 1
        if cfg.profile:
            stats.profiles.add(cfg.profile)

    conn.commit()
    return stats
