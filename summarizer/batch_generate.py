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
from summarizer.harness import HarnessConfig, _build_argv, _extract_json

_DEFAULT_CHUNK_SIZE = 25   # large chunks risk a truncated (unparseable) response
_DEFAULT_PARALLEL = 5
_DEFAULT_TIMEOUT = 900


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
# response validation
# ---------------------------------------------------------------------------

def _normalize_id(cid: str) -> str:
    """Chunk ids are extension-less repo-relative paths, but models routinely echo
    them back cosmetically mutated (``.java`` appended, backslashes, ``./`` prefix)
    — compare canonical forms so a cosmetic rewrite doesn't reject a whole chunk."""
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
) -> tuple[int, dict | None, dict]:
    """Shell out to GigaCode for one chunk; return (chunk_idx, parsed_or_None, info)."""
    prompt = _chunk_prompt(chunk_path, chunk_idx, total_chunks)
    config = HarnessConfig(
        cmd=gigacode_cmd, args=gigacode_args, prompt=prompt,
        output="stdout", timeout=timeout, cwd=cwd,
    )
    argv, err = _build_argv(config)
    if argv is None:
        return chunk_idx, None, {"error": err}

    info: dict[str, Any] = {"cmd": gigacode_cmd}
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=config.timeout, cwd=config.cwd, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        info["error"] = f"gigacode timed out after {config.timeout:.0f}s"
        return chunk_idx, None, info
    except OSError as exc:
        info["error"] = f"failed to run gigacode: {exc}"
        return chunk_idx, None, info

    info["returncode"] = proc.returncode
    # sidecar with raw stdout: debugging + what --resume checks for prior success
    sidecar = chunk_path.with_name(f"chunk-{chunk_idx:04d}-stdout.txt")
    sidecar.write_text(proc.stdout or "", encoding="utf-8")

    data = _extract_json(proc.stdout or "")
    if data is None:
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

    # 5/6. produce raw results: run GigaCode, or pick up outputs from disk ----
    project_name = original.get("project", "unknown")
    raw_results: list[tuple[int, dict | None, dict]] = []

    if args.merge_only:
        # outputs were produced elsewhere (another agent/model, manual gigacode
        # runs): just read them back — out-chunk-NNNN.json first, then the
        # gigacode sidecar chunk-NNNN-stdout.txt
        print("Merge-only mode: reading existing chunk outputs from the work dir ...")
        # ground truth for validation: the chunk files actually sent to the
        # generator, when present — re-chunking arch.json with a different
        # --chunk-size than the original run would shift every chunk boundary
        # and mass-reject perfectly good outputs
        disk_chunk_paths = sorted(work_dir.glob("chunk-????.json"))
        if disk_chunk_paths:
            try:
                disk_chunks = [
                    (json.loads(p.read_text(encoding="utf-8")) or {}).get("classes") or []
                    for p in disk_chunk_paths
                ]
            except (json.JSONDecodeError, OSError) as exc:
                print(f"WARNING: could not read chunk files ({exc}); validating against re-chunked arch.json")
            else:
                chunks = disk_chunks
                n_chunks = len(chunks)
                print(f"Validating against {n_chunks} chunk file(s) found in {work_dir}")
        for i in range(n_chunks):
            data = None
            for candidate in (work_dir / f"out-chunk-{i:04d}.json",
                              work_dir / f"chunk-{i:04d}-stdout.txt"):
                if candidate.exists():
                    data = _extract_json(candidate.read_text(encoding="utf-8", errors="replace"))
                    if data:
                        break
            raw_results.append((i, data, {}))
    else:
        for i, ch in enumerate(chunks):
            path = work_dir / f"chunk-{i:04d}.json"
            if args.resume and path.exists():
                continue
            _write_chunk(work_dir, i, _make_chunk_json(original, ch, project_name))
        print(f"Chunk files ready in {work_dir}")

        if not shutil.which(gigacode_cmd):
            print(f"\n'{gigacode_cmd}' not found on PATH — chunk files are ready for manual processing.")
            print(f"Run GigaCode on each chunk, then re-run with --resume {work_dir}")
            return

        work_items: list[tuple[int, Path]] = []
        skip_count = 0
        partial_retries = 0
        for i in range(n_chunks):
            chunk_path = work_dir / f"chunk-{i:04d}.json"
            if args.resume:
                out_path = work_dir / f"chunk-{i:04d}-stdout.txt"
                if out_path.exists():
                    existing = _extract_json(out_path.read_text(encoding="utf-8", errors="replace"))
                    _, vinfo = _validate_chunk_result(chunks[i], existing)
                    if vinfo["accepted"] and not vinfo["missing"]:
                        skip_count += 1
                        continue
                    if vinfo["accepted"]:
                        # partially described (model returned fewer classes than
                        # sent): keep what's already there, re-run the chunk so
                        # the remainder gets a second chance
                        raw_results.append((i, existing, {"resumed": True, "partial": True}))
                        partial_retries += 1
            work_items.append((i, chunk_path))
        if skip_count:
            print(f"Skipping {skip_count} already-completed chunk(s) (resume mode)")
        if partial_retries:
            print(f"Re-running {partial_retries} partially-described chunk(s), existing descriptions kept")

        print(f"Starting {len(work_items)} GigaCode session(s), {args.parallel} at a time ...\n")
        progress = _Progress(len(work_items))

        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            future_map = {
                pool.submit(_run_single_chunk, chunk_path, chunk_idx, n_chunks,
                            gigacode_cmd, gigacode_args, args.timeout, cwd): chunk_idx
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

        # resumed chunks count as prior successes: re-read their sidecars
        for i in range(n_chunks):
            if not any(r[0] == i for r in raw_results):
                out_path = work_dir / f"chunk-{i:04d}-stdout.txt"
                if out_path.exists():
                    existing = _extract_json(out_path.read_text(encoding="utf-8", errors="replace"))
                    if existing:
                        raw_results.append((i, existing, {"resumed": True}))

    # 7. validate + merge -----------------------------------------------------
    merged_classes: list[dict] = []
    failed_chunks: list[int] = []
    dropped_extraneous = 0
    missing_classes: list[str] = []

    results_by_chunk: dict[int, list[tuple[dict | None, dict]]] = {}
    for idx, data, info in raw_results:
        results_by_chunk.setdefault(idx, []).append((data, info))

    for idx in range(n_chunks):
        # fresh runs first: a retried chunk's new result claims classes before the
        # previously saved partial output fills in whatever is still missing
        entries = sorted(results_by_chunk.get(idx, []), key=lambda e: bool(e[1].get("resumed")))
        seen: set[str] = set()
        chunk_accepted: list[dict] = []
        for data, _info in entries:
            accepted, vinfo = _validate_chunk_result(chunks[idx], data, seen)
            chunk_accepted.extend(accepted)
            dropped_extraneous += vinfo["extraneous"]
        if not chunk_accepted:
            failed_chunks.append(idx)
            continue
        merged_classes.extend(chunk_accepted)
        missing_classes.extend(sorted({_class_key(c) for c in chunks[idx]} - seen))

    print(f"Merged: {len(merged_classes)}/{len(all_classes)} classes "
          f"from {n_chunks - len(failed_chunks)}/{n_chunks} chunks")
    if dropped_extraneous:
        print(f"Dropped {dropped_extraneous} extraneous/renamed class(es) returned by the model")
    if missing_classes:
        print(f"Missing (sent but not described): {len(missing_classes)} — first: {missing_classes[:5]}")
    if failed_chunks:
        print(f"Failed chunks: {failed_chunks}")
    if failed_chunks or missing_classes:
        print(f"Tip: re-run with --resume {work_dir} to retry failed and partially-described chunks")

    merged = {
        "project": project_name,
        "generated_at": original.get("generated_at", ""),
        "total_classes": len(merged_classes),
        "classes": merged_classes,
    }
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
            stats = import_flat(conn, str(repo_path), merged, source="gigacode-batch")
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
