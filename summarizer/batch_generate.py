"""Batch-generate descriptions for a flat arch.json in chunks, via parallel
GigaCode sessions.

Splits a large ``arch.json`` into chunks, runs up to ``--parallel`` GigaCode
sessions concurrently (each describing one chunk *by reading the actual Java
sources* — the ``id`` field of every class is its repo-relative source path),
validates each response against what was sent, merges the results and imports
them into the target repo's ``.reverse`` index.

Usage::

    python -m summarizer.batch_generate arch.json --repo /path/to/java-project
    python -m summarizer.batch_generate arch.json --repo ... --dry-run

The ``arch.json`` is the *source* — it gets chunked and each chunk goes to
GigaCode. ``--repo`` is the *target* project: it must have a scanned index
(``.reverse/index.sqlite3``), GigaCode sessions run with the repo as cwd so the
model can open the source files, and the results are imported there.

Import writes through :func:`analysis.flat_arch.import_flat`, so descriptions
land in ``class.summary``/``method.summary`` + the durable imported store with a
structure hash (stale ones stop winning once a class changes). Note that the
MCP tools read ONLY the SQLite index — with ``--no-import`` the results stay
invisible to an agent until ``import-arch`` is run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from analysis.flat_arch import import_flat
from index.repository import get_conn
from summarizer.harness import HarnessConfig, _build_argv, _extract_json, gigacode_available
from utils.cbmc_config import (
    cbmc_available,
    cbmc_get_code_snippet,
    cbmc_list_projects,
    cbmc_search_graph,
    resolve_cbmc_config,
)

_DEFAULT_CHUNK_SIZE = 25   # large chunks risk a truncated (unparseable) response
_DEFAULT_PARALLEL = 5
_DEFAULT_TIMEOUT = 900
_DEFAULT_CBMC_TIMEOUT = 30.0
_CBMC_FETCH_PARALLEL = 8   # per-chunk code-snippet fetches; independent of --parallel


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------

def _chunk_classes(classes: list[dict], chunk_size: int) -> list[list[dict]]:
    return [classes[i: i + chunk_size] for i in range(0, len(classes), chunk_size)]


def _make_chunk_json(original: dict, chunk: list[dict], project: str | None = None) -> dict:
    """Wrap a chunk of classes back into the flat-architecture envelope so that
    GigaCode recognises it and ``import_flat`` can consume the result."""
    return {
        "project": project or original.get("project", "unknown"),
        "generated_at": original.get("generated_at", ""),
        "total_classes": len(chunk),
        "classes": chunk,
    }


def _write_chunk(chunk_dir: Path, idx: int, data: dict) -> Path:
    path = chunk_dir / f"chunk-{idx:04d}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


_CHUNK_FILE_RE = re.compile(r"chunk-(\d+)\.json$")


def _chunk_file_index(path: Path) -> int | None:
    """The numeric index encoded in a ``chunk-NNNN.json`` file name (None if it
    doesn't match). Used to key chunks by their real number, not their position in
    a sorted glob — a gap in the numbering must not shift every later chunk."""
    m = _CHUNK_FILE_RE.search(path.name)
    return int(m.group(1)) if m else None


def _load_disk_chunks(work_dir: Path) -> dict[int, list[dict]]:
    """Map ``chunk_index -> classes`` from the chunk files actually written to disk.
    These are the ground truth for validation: they are exactly what was sent to the
    generator, regardless of the current ``--chunk-size`` or a re-chunked arch.json."""
    out: dict[int, list[dict]] = {}
    for p in sorted(work_dir.glob("chunk-????.json")):
        idx = _chunk_file_index(p)
        if idx is None:
            continue
        try:
            out[idx] = (json.loads(p.read_text(encoding="utf-8")) or {}).get("classes") or []
        except (json.JSONDecodeError, OSError):
            continue
    return out


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

def _chunk_prompt(chunk_path: Path, idx: int, total: int) -> str:
    """A GigaCode prompt for one chunk. Two rules matter for the consuming agent:
    describe from the *real source* (id = repo-relative path), and never invent —
    a confident hallucinated description misleads the agent worse than no text."""
    return (
        f"Открой файл {chunk_path} — это фрагмент архитектуры Java-проекта "
        f"(часть {idx + 1} из {total}) в flat-JSON формате. "
        "Поле id каждого класса — путь к его исходнику относительно корня проекта "
        "БЕЗ расширения: чтобы найти файл, добавь к id расширение .java, но сам id "
        "нигде не переписывай. Для КАЖДОГО класса сначала открой и прочитай "
        "его исходный файл, затем заполни поле description на русском языке: что "
        "делает класс, зачем он нужен, какую бизнес-задачу решает; укажи побочные "
        "эффекты (запись в БД, отправка в Kafka, вызовы внешних API, транзакционность) "
        "и инварианты, которые нужно сохранить при изменении кода. Для каждого метода — "
        "1-2 предложения о его логике по реальному коду. Если исходник не найден или "
        "логика неясна — опиши по сигнатуре и скажи об этом; ничего не выдумывай. "
        "Верни ТОЛЬКО JSON в том же формате с заполненными description. Верни ВСЕ "
        "классы входного файла — ответ с меньшим числом классов считается ошибкой. "
        "id, pkg, name и sig скопируй в точности как во входном файле (id — без "
        ".java): не изменяй id и сигнатуры, не добавляй и не удаляй классы и методы, "
        "без markdown и пояснений."
    )


# ---------------------------------------------------------------------------
# codebase-memory-mcp (CBMC) context — Layer-1 grounding for descriptions
#
# Instead of telling the generator "open file X and read it" (an agentic round-trip
# that a text-only generator can't do at all), we pull the real source of each class
# straight from the CBMC knowledge graph and inline it into the prompt. The model
# then describes from actual code + its graph neighbours, not from a bare signature.
# CBMC is entirely optional: any failure degrades to the file-based prompt, per class.
# ---------------------------------------------------------------------------

def _cls_fqn(cls: dict) -> str:
    """Reconstruct the fully-qualified name arch.json carries (pkg + name)."""
    pkg, name = cls.get("pkg", ""), cls.get("name", "")
    return f"{pkg}.{name}" if pkg and name else name


def _resolve_cbmc_project(
    repo_path: str, config: dict | None = None, binary: str | None = None
) -> str | None:
    """Map a repo path to a CBMC project name — explicit config wins over heuristics.

    CBMC keys projects by a name derived from the indexed path. Guessing it by fuzzy
    substring (as an earlier draft did) risks binding to the *wrong* project and
    silently grounding descriptions in someone else's code, so we only accept an
    exact match or a *unique* substring match; anything ambiguous returns None and
    the caller falls back to file mode rather than guess. ``binary`` must be passed
    through — a --cbmc-bin binary that isn't on PATH would otherwise make the
    project listing silently come back empty."""
    # 1. explicit override: env var or legacy-reverse.toml [cbmc] project
    explicit = os.environ.get("LEGACY_REVERSE_CBMC_PROJECT") or (config or {}).get("project")
    if explicit:
        return explicit

    projects, _info = cbmc_list_projects(binary=binary)
    names = [p.get("name", "") for p in projects if p.get("name")]
    if not names:
        return None

    repo = Path(repo_path).resolve()
    # 2. exact path-based name (CBMC default: path with separators replaced by '-')
    expected = repo.as_posix().replace("/", "-")
    if expected in names:
        return expected

    # 3. unique case-insensitive substring match on the repo folder name
    repo_name = repo.name.lower()
    subs = [n for n in names if repo_name in n.lower() or n.lower() in repo_name]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        print(f"  CBMC: ambiguous project for '{repo.name}': {subs}; "
              "set [cbmc] project or LEGACY_REVERSE_CBMC_PROJECT — using file mode")
    return None


def _extract_snippet(result: dict) -> tuple[str, list[str]]:
    """(source, related_qualified_names) from a get_code_snippet result."""
    src = (result.get("source") or "").strip()
    neighbors = result.get("neighbors") or []
    related = [n.get("qualified_name", "") for n in neighbors[:5] if n.get("qualified_name")]
    return src, related


def _fetch_class_code(
    cls: dict, project: str, binary: str | None, cbmc_timeout: float
) -> dict | None:
    """Fetch one class's source from CBMC. FQN (pkg+name from arch.json) is the exact,
    reliable key; only if it doesn't resolve do we fall back to a semantic search and
    re-fetch by the resolved qualified name. Returns None (→ prompt degrades to the
    signature for this class) when no code can be grounded."""
    fqn = _cls_fqn(cls)
    if not fqn:
        return None

    result, _info = cbmc_get_code_snippet(
        fqn, project=project, include_neighbors=True, binary=binary, timeout=cbmc_timeout
    )
    if result:
        src, related = _extract_snippet(result)
        if src:
            return {"fqn": fqn, "code": src, "related": related}

    # fallback: arch.json name may differ from the graph's canonical qualified_name
    name = cls.get("name", "")
    sres, _ = cbmc_search_graph(name, project=project, binary=binary) if name else (None, {})
    matches = (sres or {}).get("results") or (sres or {}).get("matches") or []
    qn = next(
        (m.get("qualified_name", "") for m in matches
         if isinstance(m, dict) and m.get("qualified_name")),
        "",
    )
    if qn and qn != fqn:
        result2, _ = cbmc_get_code_snippet(
            qn, project=project, include_neighbors=True, binary=binary, timeout=cbmc_timeout
        )
        if result2:
            src, related = _extract_snippet(result2)
            if src:
                return {"fqn": fqn, "code": src, "related": related}
    return None


def _fetch_chunk_context(
    chunk_classes: list[dict], project: str, binary: str | None, cbmc_timeout: float
) -> dict[str, dict]:
    """Batch-fetch code for a chunk's classes via CBMC. Maps ``fqn -> {code, related}``
    for the classes that grounded; failures are logged (not silently swallowed) and
    simply absent from the map."""
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_CBMC_FETCH_PARALLEL) as pool:
        futures = {
            pool.submit(_fetch_class_code, cls, project, binary, cbmc_timeout): cls
            for cls in chunk_classes
        }
        for fut in as_completed(futures):
            cls = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - one class must not sink the chunk
                print(f"  CBMC fetch failed for {_cls_fqn(cls)}: {exc}")
                continue
            if res:
                results[res["fqn"]] = res
    return results


_MAX_SNIPPET_CHARS = 8000  # per-class source cap; 5-class chunks stay ~10K tokens


def _cbmc_chunk_prompt(
    chunk_classes: list[dict], idx: int, total: int, ctx: dict[str, dict]
) -> str:
    """A prompt carrying the real code of each class inline (from CBMC) plus its graph
    neighbours. Classes CBMC couldn't ground degrade to their signature, marked so the
    model knows to describe conservatively rather than invent.

    Three rules keep the response validatable: every class block leads with a
    "Метаданные" JSON line so the id/pkg/name/sig echo the validator matches on is
    *copyable, not guessable*; the method list to describe is spelled out (else the
    model describes whatever it sees in the source — private helpers the index
    doesn't know, missed overloads); and per-class source is capped so one huge
    class can't push the whole chunk into a truncated, unparseable response."""
    lines = [
        f"Сгенерируй описания на русском для {len(chunk_classes)} классов Java-проекта "
        f"(часть {idx + 1} из {total}).",
        "Для каждого класса даны: строка 'Метаданные' (JSON), список 'Методы' и код "
        "из knowledge graph.",
        "Описывай ТОЛЬКО по коду. Если код не дан — опиши по сигнатуре и скажи об этом; "
        "ничего не выдумывай.",
        "",
    ]
    for cls in chunk_classes:
        fqn = _cls_fqn(cls)
        info = ctx.get(fqn)
        meta = {k: cls.get(k) for k in ("id", "pkg", "name", "sig") if cls.get(k) is not None}
        method_sigs = [m.get("sig") for m in cls.get("methods") or [] if m.get("sig")]
        lines.append(f"--- {fqn} ---")
        lines.append(f"Метаданные: {json.dumps(meta, ensure_ascii=False)}")
        if method_sigs:
            lines.append(f"Методы: {json.dumps(method_sigs, ensure_ascii=False)}")
        if info and info.get("code"):
            code = info["code"]
            if len(code) > _MAX_SNIPPET_CHARS:
                code = code[:_MAX_SNIPPET_CHARS] + "\n… [код усечён]"
            lines.append("[Код из knowledge graph]")
            lines.append(code)
            related = info.get("related") or []
            if related:
                lines.append(f"[Связанные классы (только контекст, их описывать не нужно): "
                             f"{', '.join(related)}]")
        else:
            lines.append("[Код не найден в knowledge graph — опиши по сигнатуре и скажи об этом]")
        lines.append("")

    lines.append(
        "Верни ТОЛЬКО JSON в формате "
        '{"classes": [{"id": "...", "pkg": "...", "name": "...", "sig": "...", '
        '"description": "...", "methods": [{"sig": "...", "description": "..."}]}]}.'
    )
    lines.append(
        "Поля id, pkg, name, sig каждого класса скопируй В ТОЧНОСТИ из его строки "
        "'Метаданные'; sig каждого метода — из списка 'Методы'. Опиши каждый метод из "
        "списка 1-2 предложениями по реальному коду; не добавляй методы, которых нет в "
        "списке, и не пропускай перечисленные. "
        "В description на русском: что делает класс, зачем он нужен, какую бизнес-задачу "
        "решает; укажи побочные эффекты (запись в БД, отправка в Kafka, вызовы внешних "
        "API, транзакционность) и инварианты, которые нужно сохранить при изменении кода. "
        f"Верни ВСЕ {len(chunk_classes)} классов — ответ с меньшим числом классов "
        "считается ошибкой. Без markdown и пояснений."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# response validation
# ---------------------------------------------------------------------------

def _normalize_id(cid: str) -> str:
    """Chunk ids are extension-less repo-relative paths, but models routinely echo
    them back cosmetically mutated (``.java`` appended, backslashes, ``./`` prefix)
    — compare canonical forms so a cosmetic rewrite doesn't reject a whole chunk."""
    if not isinstance(cid, str):
        return str(cid)
    cid = cid.replace("\\", "/").strip()
    while cid.startswith("./"):
        cid = cid[2:]
    return cid[:-5] if cid.endswith(".java") else cid


def _class_key(c: dict) -> str:
    cid = c.get("id")
    return _normalize_id(str(cid)) if cid else f"{c.get('pkg')}.{c.get('name')}"


def _validate_chunk_result(
    sent: list[dict], returned: dict | None, seen: set[str] | None = None
) -> tuple[list[dict], dict]:
    """Keep only returned classes that correspond to classes actually sent
    (matched by normalized id, falling back to pkg+name). A model that renamed,
    invented or dropped classes must not smuggle descriptions onto the wrong
    symbols — ``import_flat`` matches leniently by simple name, so garbage in
    would stick. ``seen`` is a cross-call accumulator: several results for the
    same chunk (a partial first attempt + its retry) validate as a union without
    double-accepting; ``missing`` then reflects what the union still lacks."""
    seen = seen if seen is not None else set()
    info: dict[str, Any] = {"sent": len(sent), "returned": 0, "accepted": 0,
                            "extraneous": 0, "missing": []}
    sent_keys = {_class_key(c) for c in sent}
    name_to_key = {f"{c.get('pkg')}.{c.get('name')}": _class_key(c) for c in sent}
    if not returned or not isinstance(returned.get("classes"), list):
        info["missing"] = sorted(sent_keys - seen)
        return [], info

    accepted: list[dict] = []
    for c in returned["classes"]:
        if not isinstance(c, dict):
            continue
        info["returned"] += 1
        k = _class_key(c)
        if k not in sent_keys:
            # model rewrote the id beyond cosmetics (absolute path, wrong root):
            # pkg+name still pins the class — the same identity import_flat uses
            k = name_to_key.get(f"{c.get('pkg')}.{c.get('name')}", "")
        if not k or k not in sent_keys:
            info["extraneous"] += 1
        elif k in seen:
            continue  # already covered by an earlier attempt for this chunk
        else:
            accepted.append(c)
            seen.add(k)
    info["accepted"] = len(accepted)
    info["missing"] = sorted(sent_keys - seen)
    return accepted, info


# ---------------------------------------------------------------------------
# per-chunk runner (used by ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _run_single_chunk(
    chunk_path: Path,
    chunk_idx: int,
    total_chunks: int,
    gigacode_cmd: str,
    gigacode_args: list[str],
    timeout: float,
    cwd: str | None,
    *,
    use_cbmc: bool = False,
    cbmc_project: str | None = None,
    cbmc_binary: str | None = None,
    cbmc_timeout: float = _DEFAULT_CBMC_TIMEOUT,
    chunk_classes: list[dict] | None = None,
) -> tuple[int, dict | None, dict]:
    """Shell out to GigaCode for one chunk; return (chunk_idx, parsed_or_None, info).

    With ``use_cbmc`` and a resolved project, the prompt inlines each class's real
    source pulled from CBMC (Layer 1). Grounding is per-class: whatever CBMC can't
    resolve degrades to its signature *within the same enriched prompt* — we don't
    throw away a chunk's worth of grounded code just because one class was missing.
    Only a total miss (nothing grounded) reverts the chunk to the file-based prompt."""
    grounded: int | None = None
    if use_cbmc and cbmc_project and chunk_classes:
        ctx = _fetch_chunk_context(chunk_classes, cbmc_project, cbmc_binary, cbmc_timeout)
        grounded = len(ctx)
        if ctx:
            prompt = _cbmc_chunk_prompt(chunk_classes, chunk_idx, total_chunks, ctx)
        else:
            prompt = _chunk_prompt(chunk_path, chunk_idx, total_chunks)
    else:
        prompt = _chunk_prompt(chunk_path, chunk_idx, total_chunks)
    config = HarnessConfig(
        cmd=gigacode_cmd, args=gigacode_args, prompt=prompt,
        output="stdout", timeout=timeout, cwd=cwd,
    )
    argv, err = _build_argv(config)
    if argv is None:
        return chunk_idx, None, {"error": err}

    info: dict[str, Any] = {"cmd": gigacode_cmd}
    if grounded is not None:
        info["cbmc_grounded"] = grounded
        info["cbmc_total"] = len(chunk_classes or [])
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=config.timeout, cwd=config.cwd,
        )
    except subprocess.TimeoutExpired:
        info["error"] = f"gigacode timed out after {config.timeout:.0f}s"
        return chunk_idx, None, info
    except OSError as exc:
        info["error"] = f"failed to run gigacode: {exc}"
        return chunk_idx, None, info

    info["returncode"] = proc.returncode
    # sidecar with raw stdout: debugging + what --resume checks for prior success.
    # Only overwrite it when the new output actually parses — otherwise an
    # unparseable retry would clobber a previous good sidecar, and the next
    # --resume would treat the chunk as never described (re-spending the LLM).
    sidecar = chunk_path.with_name(f"chunk-{chunk_idx:04d}-stdout.txt")
    data = _extract_json(proc.stdout or "")
    if data is not None:
        sidecar.write_text(proc.stdout or "", encoding="utf-8")
    else:
        # keep the failed output beside the good sidecar for debugging, not over it
        sidecar.with_name(f"chunk-{chunk_idx:04d}-stdout.err.txt").write_text(
            proc.stdout or "", encoding="utf-8"
        )
        info["error"] = "gigacode produced no parseable flat JSON"
        info["stderr_tail"] = (proc.stderr or "").strip()[-400:]
    return chunk_idx, data, info


# ---------------------------------------------------------------------------
# progress display
# ---------------------------------------------------------------------------

class _Progress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.ok = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._start = time.time()

    def tick(self, success: bool) -> None:
        with self._lock:
            self.done += 1
            if success:
                self.ok += 1
            else:
                self.failed += 1
            elapsed = time.time() - self._start
        sys.stdout.write(
            f"\r[{self.done}/{self.total}] ok={self.ok} fail={self.failed} elapsed={elapsed:.0f}s"
        )
        sys.stdout.flush()

    def done_line(self) -> str:
        elapsed = time.time() - self._start
        return f"\nDone: {self.ok} ok, {self.failed} failed, {self.total} total in {elapsed:.0f}s"


# ---------------------------------------------------------------------------
# main orchestration
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-generate architecture descriptions via parallel GigaCode sessions."
    )
    parser.add_argument(
        "arch_json",
        help="Path to the arch.json to process (the *source*: chunked and fed to GigaCode).",
    )
    parser.add_argument(
        "--repo", required=True,
        help="Path to the target Java project (has .reverse/index.sqlite3; GigaCode runs "
             "with this as cwd so it can read the sources; results are imported here).",
    )
    parser.add_argument("--chunk-size", type=int, default=_DEFAULT_CHUNK_SIZE,
                        help=f"Classes per chunk (default: {_DEFAULT_CHUNK_SIZE}).")
    parser.add_argument("--parallel", type=int, default=_DEFAULT_PARALLEL,
                        help=f"Concurrent GigaCode sessions (default: {_DEFAULT_PARALLEL}).")
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT,
                        help=f"Per-chunk timeout in seconds (default: {_DEFAULT_TIMEOUT}).")
    parser.add_argument("--gigacode-cmd", default=None,
                        help="GigaCode command (default: LEGACY_REVERSE_GIGACODE_CMD or 'gigacode').")
    parser.add_argument("--gigacode-args", default=None,
                        help="Space-separated GigaCode args (default: '-p').")
    parser.add_argument("--use-cbmc", action="store_true",
                        help="Ground each chunk in real source pulled from the codebase-memory-mcp "
                             "knowledge graph (Layer 1) before calling the generator, instead of "
                             "asking it to open files. Improves quality for text-only generators "
                             "and removes file-opening round-trips. Degrades to file mode per class "
                             "when CBMC is unavailable or a class isn't in the graph.")
    parser.add_argument("--cbmc-bin", default=None,
                        help="Path to the codebase-memory-mcp binary (default: LEGACY_REVERSE_CBMC_BIN "
                             "or legacy-reverse.toml [cbmc] binary_path or PATH).")
    parser.add_argument("--cbmc-timeout", type=float, default=_DEFAULT_CBMC_TIMEOUT,
                        help=f"Per-class CBMC fetch timeout in seconds (default: {_DEFAULT_CBMC_TIMEOUT:.0f}).")
    parser.add_argument("--work-dir", default=None,
                        help="Working directory for chunk files (default: <repo>/.reverse/batch).")
    parser.add_argument("--no-import", action="store_true",
                        help="Skip importing into the index. NOTE: MCP tools read only the "
                             "index — without a later import-arch the agent sees nothing.")
    parser.add_argument("--merge-only", action="store_true",
                        help="Don't run GigaCode: validate/merge/import chunk outputs that are "
                             "already in the work dir (out-chunk-NNNN.json, e.g. produced by "
                             "another agent, or chunk-NNNN-stdout.txt sidecars).")
    parser.add_argument("--skip-describe", action="store_true",
                        help="Skip the post-import describe pass (it rebuilds package/module/"
                             "project summaries from the imported class descriptions).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show chunk layout and exit without running GigaCode.")
    parser.add_argument("--keep-chunks", action="store_true",
                        help="Keep chunk files after completion (for debugging).")
    parser.add_argument("--resume", metavar="DIR", default=None,
                        help="Resume from a previous --work-dir (skips already-succeeded chunks).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:  # noqa: C901 - linear orchestration script
    args = parse_args(argv)

    # 1. load the full arch.json -------------------------------------------
    arch_path = Path(args.arch_json)
    if not arch_path.exists():
        print(f"ERROR: {arch_path} not found")
        sys.exit(1)
    original: dict = json.loads(arch_path.read_text(encoding="utf-8"))
    all_classes: list[dict] = original.get("classes") or []
    print(f"Loaded {len(all_classes)} classes from {arch_path}")

    # 2. split into chunks ---------------------------------------------------
    chunks = _chunk_classes(all_classes, args.chunk_size)
    n_chunks = len(chunks)
    print(f"Split into {n_chunks} chunks (chunk-size={args.chunk_size})")

    if args.dry_run:
        for i, ch in enumerate(chunks):
            sample = ch[0].get("name") if ch else "(empty)"
            print(f"  chunk {i:4d}: {len(ch):4d} classes  (first: {sample})")
        print("\nDry-run mode — no GigaCode sessions launched.")
        return

    # 3. working directory (or resume) --------------------------------------
    repo_path = Path(args.repo).resolve()
    cwd = os.environ.get("LEGACY_REVERSE_GIGACODE_CWD") or str(repo_path)
    work_dir = Path(args.work_dir) if args.work_dir else repo_path / ".reverse" / "batch"
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        rs_dir = Path(args.resume)
        if rs_dir != work_dir:
            print(f"WARNING: --resume={rs_dir} != work-dir={work_dir}; copying resume files over")
            for f in rs_dir.glob("chunk-*.json"):
                shutil.copy2(f, work_dir / f.name)
            for f in rs_dir.glob("chunk-*-stdout.txt"):
                shutil.copy2(f, work_dir / f.name)
        print(f"Resume mode: working directory = {work_dir}")

    # 4. gigacode config ------------------------------------------------------
    gigacode_cmd = args.gigacode_cmd or os.environ.get("LEGACY_REVERSE_GIGACODE_CMD", "gigacode")
    raw_args = args.gigacode_args or os.environ.get("LEGACY_REVERSE_GIGACODE_ARGS", "-p")
    gigacode_args = raw_args.split()

    # 4b. codebase-memory-mcp (Layer-1 grounding) — resolve once, degrade cleanly ----
    use_cbmc = args.use_cbmc
    cbmc_binary: str | None = None
    cbmc_project: str | None = None
    if use_cbmc:
        cbmc_binary, cbmc_cfg = (args.cbmc_bin, {}) if args.cbmc_bin else resolve_cbmc_config(str(repo_path))
        if not cbmc_available(cbmc_binary):
            print(f"--use-cbmc: '{cbmc_binary}' unavailable — falling back to file mode")
            use_cbmc = False
        else:
            cbmc_project = _resolve_cbmc_project(str(repo_path), cbmc_cfg, binary=cbmc_binary)
            if not cbmc_project:
                print("--use-cbmc: could not resolve a CBMC project — falling back to file mode")
                use_cbmc = False
            else:
                print(f"CBMC grounding enabled: binary={cbmc_binary}, project={cbmc_project}")

    # 5/6. produce raw results: run GigaCode, or pick up outputs from disk ----
    project_name = original.get("project", "unknown")
    raw_results: list[tuple[int, dict | None, dict]] = []

    # Ground truth for validation, keyed by real chunk index. A fresh run uses the
    # slicing just computed; --resume / --merge-only prefer the chunk files already
    # on disk (exactly what was sent), so a changed --chunk-size or a re-generated
    # arch.json can't shift boundaries and mass-reject good outputs (and a gap in
    # the numbering can't shift later chunks — indices come from the file names).
    disk_by_idx = _load_disk_chunks(work_dir)
    if (args.resume or args.merge_only) and disk_by_idx:
        sent_by_idx = disk_by_idx
    else:
        sent_by_idx = {i: ch for i, ch in enumerate(chunks)}
    chunk_indices = sorted(sent_by_idx)
    total_chunks = len(chunk_indices)

    if args.merge_only:
        # outputs were produced elsewhere (another agent/model, manual gigacode
        # runs): just read them back — out-chunk-NNNN.json first, then the
        # gigacode sidecar chunk-NNNN-stdout.txt
        print("Merge-only mode: reading existing chunk outputs from the work dir ...")
        if disk_by_idx:
            print(f"Validating against {len(disk_by_idx)} chunk file(s) found in {work_dir}")
        for idx in chunk_indices:
            data = None
            for candidate in (work_dir / f"out-chunk-{idx:04d}.json",
                              work_dir / f"chunk-{idx:04d}-stdout.txt"):
                if candidate.exists():
                    data = _extract_json(candidate.read_text(encoding="utf-8", errors="replace"))
                    if data:
                        break
            raw_results.append((idx, data, {}))
    else:
        for idx in chunk_indices:
            path = work_dir / f"chunk-{idx:04d}.json"
            if args.resume and path.exists():
                continue
            _write_chunk(work_dir, idx, _make_chunk_json(original, sent_by_idx[idx], project_name))
        print(f"Chunk files ready in {work_dir}")

        # accept a gigacode installed only via GIGACODE/GIGACODE_CLI env (no PATH
        # entry) — same availability rule the harness/MCP generate uses
        if not gigacode_available(gigacode_cmd):
            print(f"\n'{gigacode_cmd}' not found on PATH or via GIGACODE/GIGACODE_CLI — "
                  "chunk files are ready for manual processing.")
            print(f"Run GigaCode on each chunk, then re-run with --resume {work_dir}")
            return

        work_items: list[tuple[int, Path]] = []
        skip_count = 0
        partial_retries = 0
        for idx in chunk_indices:
            chunk_path = work_dir / f"chunk-{idx:04d}.json"
            if args.resume:
                out_path = work_dir / f"chunk-{idx:04d}-stdout.txt"
                if out_path.exists():
                    existing = _extract_json(out_path.read_text(encoding="utf-8", errors="replace"))
                    _, vinfo = _validate_chunk_result(sent_by_idx[idx], existing)
                    if vinfo["accepted"] and not vinfo["missing"]:
                        # fully described already — record it as a prior success and
                        # don't re-run (no separate re-read pass needed afterwards)
                        raw_results.append((idx, existing, {"resumed": True}))
                        skip_count += 1
                        continue
                    if vinfo["accepted"]:
                        # partially described (model returned fewer classes than
                        # sent): keep what's already there, re-run the chunk so
                        # the remainder gets a second chance
                        raw_results.append((idx, existing, {"resumed": True, "partial": True}))
                        partial_retries += 1
            work_items.append((idx, chunk_path))
        if skip_count:
            print(f"Skipping {skip_count} already-completed chunk(s) (resume mode)")
        if partial_retries:
            print(f"Re-running {partial_retries} partially-described chunk(s), existing descriptions kept")

        print(f"Starting {len(work_items)} GigaCode session(s), {args.parallel} at a time ...\n")
        progress = _Progress(len(work_items))

        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            future_map = {
                pool.submit(_run_single_chunk, chunk_path, chunk_idx, total_chunks,
                            gigacode_cmd, gigacode_args, args.timeout, cwd,
                            use_cbmc=use_cbmc, cbmc_project=cbmc_project,
                            cbmc_binary=cbmc_binary, cbmc_timeout=args.cbmc_timeout,
                            chunk_classes=sent_by_idx[chunk_idx]): chunk_idx
                for chunk_idx, chunk_path in work_items
            }
            for fut in as_completed(future_map):
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001 - keep the batch going
                    result = (future_map[fut], None, {"error": f"unexpected exception: {exc}"})
                raw_results.append(result)
                progress.tick(result[1] is not None)
        print(progress.done_line())

        # CBMC grounding summary (kept out of the \r progress line to avoid clobber)
        if use_cbmc:
            g = sum(i.get("cbmc_grounded", 0) for _, _, i in raw_results)
            t = sum(i.get("cbmc_total", 0) for _, _, i in raw_results if "cbmc_total" in i)
            if t:
                print(f"CBMC: grounded {g}/{t} classes in real source "
                      f"({t - g} described from signature only)")

    # 7. validate + merge -----------------------------------------------------
    merged_classes: list[dict] = []
    failed_chunks: list[int] = []
    dropped_extraneous = 0
    missing_classes: list[str] = []

    results_by_chunk: dict[int, list[tuple[dict | None, dict]]] = {}
    for idx, data, info in raw_results:
        results_by_chunk.setdefault(idx, []).append((data, info))

    for idx in chunk_indices:
        # fresh runs first: a retried chunk's new result claims classes before the
        # previously saved partial output fills in whatever is still missing
        entries = sorted(results_by_chunk.get(idx, []), key=lambda e: bool(e[1].get("resumed")))
        seen: set[str] = set()
        chunk_accepted: list[dict] = []
        for data, _info in entries:
            accepted, vinfo = _validate_chunk_result(sent_by_idx[idx], data, seen)
            chunk_accepted.extend(accepted)
            dropped_extraneous += vinfo["extraneous"]
        if not chunk_accepted:
            failed_chunks.append(idx)
            continue
        merged_classes.extend(chunk_accepted)
        missing_classes.extend(sorted({_class_key(c) for c in sent_by_idx[idx]} - seen))

    print(f"Merged: {len(merged_classes)}/{len(all_classes)} classes "
          f"from {total_chunks - len(failed_chunks)}/{total_chunks} chunks")
    if dropped_extraneous:
        print(f"Dropped {dropped_extraneous} extraneous/renamed class(es) returned by the model")
    if missing_classes:
        print(f"Missing (sent but not described): {len(missing_classes)} — first: {missing_classes[:5]}")
    if failed_chunks:
        print(f"Failed chunks: {failed_chunks}")
    if failed_chunks or missing_classes:
        print(f"Tip: re-run with --resume {work_dir} to retry failed and partially-described chunks")

    merged = _make_chunk_json(original, merged_classes, project_name)
    # keep the merged artifact under .reverse/ (gitignored), not the repo root
    merged_path = repo_path / ".reverse" / "arch-merged.json"
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Merged JSON written to: {merged_path}")

    # 8. import into the index -------------------------------------------------
    if args.no_import:
        print("\nSkipping import (--no-import).")
        print("NOTE: the MCP tools read only the SQLite index — the agent will not see")
        print("these descriptions until you run:")
        print(f"  legacy-reverse import-arch --repo {repo_path} {merged_path}")
    else:
        db_path = repo_path / ".reverse" / "index.sqlite3"
        if not db_path.exists():
            print(f"ERROR: index DB not found at {db_path}. Run 'scan' first.")
            sys.exit(1)
        conn = get_conn(db_path)
        try:
            import_source = "gigacode-batch+cbmc" if use_cbmc else "gigacode-batch"
            stats = import_flat(conn, str(repo_path), merged, source=import_source)
            print("\n=== Import results ===")
            print(f"  Classes matched:   {stats['classes_matched']}/{stats['classes_total']}")
            print(f"  Methods matched:   {stats['methods_matched']}")
            print(f"  Methods unmatched: {stats['methods_unmatched']}")
            if stats.get("unmatched_classes"):
                print(f"  Unmatched classes: {', '.join(stats['unmatched_classes'][:10])}")

            # 8b. hierarchy: flat JSON has no package/module/project level; a
            # describe pass aggregates the freshly imported class descriptions
            # into those summaries (imported wins per class -> no LLM re-spend).
            if not args.skip_describe:
                from summarizer.describe import describe_repo
                print("\nRebuilding package/module/project summaries (describe pass) ...")
                describe_repo(conn, str(repo_path), progress=print)
        finally:
            conn.close()

    # 9. cleanup ---------------------------------------------------------------
    # merge-only outputs were produced by someone else — never delete them here;
    # missing classes need the work dir intact for a --resume retry
    keep = args.keep_chunks or bool(failed_chunks) or bool(missing_classes) or args.merge_only
    if keep:
        print(f"Chunk files preserved in: {work_dir}")
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
        print(f"Working directory cleaned up: {work_dir}")


if __name__ == "__main__":
    main()
