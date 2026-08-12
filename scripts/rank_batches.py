#!/usr/bin/env python3
"""Rank every generated Batch using cheap deterministic repository metadata.

This is a scheduler only. It does NOT declare a vulnerability.
Every Batch remains in the queue exactly once; higher-scoring Batches are
analyzed earlier by the LLM runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="\n") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object {path}:{line_number}")
            yield value


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


# Generic security-relevant API families. These are scheduling hints only.
# No Batch is skipped based on these names.
SENSITIVE_EXACT: dict[str, tuple[str, int]] = {
    # classic memory/string operations
    "strcpy": ("memory_string", 12),
    "strcat": ("memory_string", 12),
    "sprintf": ("memory_string", 10),
    "vsprintf": ("memory_string", 10),
    "gets": ("memory_string", 14),
    "memcpy": ("memory_copy", 7),
    "memmove": ("memory_copy", 6),
    "memset": ("memory_copy", 3),
    "sscanf": ("formatted_parse", 7),
    "scanf": ("formatted_parse", 7),
    "fscanf": ("formatted_parse", 6),

    # external input / files / network-ish primitives
    "read": ("external_input", 5),
    "recv": ("external_input", 7),
    "recvfrom": ("external_input", 7),
    "fread": ("external_input", 5),
    "getline": ("external_input", 4),
    "getenv": ("external_input", 4),
    "open": ("file_path", 4),
    "fopen": ("file_path", 4),
    "stat": ("file_path", 3),
    "lstat": ("file_path", 4),
    "unlink": ("file_path", 4),
    "rename": ("file_path", 4),

    # process / command execution
    "system": ("command_exec", 15),
    "popen": ("command_exec", 14),
    "execl": ("command_exec", 15),
    "execv": ("command_exec", 15),
    "execve": ("command_exec", 15),
    "execvp": ("command_exec", 15),

    # allocation / size / conversion
    "malloc": ("memory_lifetime", 4),
    "calloc": ("memory_lifetime", 4),
    "realloc": ("memory_lifetime", 6),
    "free": ("memory_lifetime", 3),
    "strlen": ("size_handling", 3),
    "strtol": ("numeric_parse", 4),
    "strtoul": ("numeric_parse", 4),
    "atoi": ("numeric_parse", 4),
    "ioctl": ("device_boundary", 7),
}

PREFIX_RULES: tuple[tuple[str, str, int], ...] = (
    ("exec", "command_exec", 13),
    ("strto", "numeric_parse", 4),
)


def sensitive_api(name: str) -> tuple[str, int] | None:
    simple = name.rsplit("::", 1)[-1].strip()
    if simple in SENSITIVE_EXACT:
        return SENSITIVE_EXACT[simple]
    for prefix, category, weight in PREFIX_RULES:
        if simple.startswith(prefix):
            return category, weight
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/priority_manifest.jsonl"),
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("artifacts/evaluation/priority_summary.json"),
    )
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    root = args.root.resolve()
    batch_path = root / "artifacts" / "chunking" / "batch_manifest.jsonl"
    chunk_path = root / "artifacts" / "chunking" / "chunk_manifest.jsonl"
    call_path = root / "artifacts" / "index" / "call_edges.jsonl"
    symbol_path = root / "artifacts" / "index" / "symbol_index.jsonl"

    for path in (batch_path, chunk_path, call_path, symbol_path):
        if not path.is_file():
            raise ValueError(f"Required artifact missing: {path}")

    batches = list(iter_jsonl(batch_path))
    chunk_to_path = {
        record["chunk_id"]: record["path"]
        for record in iter_jsonl(chunk_path)
    }

    symbol_to_path: dict[str, str] = {}
    for record in iter_jsonl(symbol_path):
        symbol_to_path[record["symbol_id"]] = record["path"]

    calls_by_chunk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in iter_jsonl(call_path):
        calls_by_chunk[record["reference"]["anchor"]["chunk_id"]].append(record)

    scored: list[dict[str, Any]] = []

    for batch in batches:
        batch_id = batch["batch_id"]
        chunk_ids = list(batch["chunk_ids"])
        batch_paths = {chunk_to_path.get(cid, "") for cid in chunk_ids}
        calls: list[dict[str, Any]] = []
        for cid in chunk_ids:
            calls.extend(calls_by_chunk.get(cid, ()))

        categories: Counter[str] = Counter()
        sensitive_names: Counter[str] = Counter()
        sensitive_raw = 0
        cross_file_targets: set[str] = set()
        uncertain_calls = 0
        indirect_calls = 0

        for call in calls:
            callee_name = str(call.get("callee_name", ""))
            hit = sensitive_api(callee_name)
            if hit is not None:
                category, weight = hit
                categories[category] += 1
                sensitive_names[callee_name] += 1
                sensitive_raw += weight

            resolution = str(call.get("resolution", ""))
            if resolution in {
                "AMBIGUOUS",
                "MACRO_OR_FUNCTION_AMBIGUOUS",
                "MEMBER_OR_QUALIFIED",
            }:
                uncertain_calls += 1
            if resolution == "INDIRECT_OR_DYNAMIC":
                indirect_calls += 1

            for symbol_id in call.get("candidate_definition_ids", []):
                target_path = symbol_to_path.get(symbol_id)
                if target_path and target_path not in batch_paths:
                    cross_file_targets.add(symbol_id)

        # Score is deliberately bounded and explainable.
        # It prioritizes analysis; it is not a vulnerability probability.
        components = {
            "sensitive_api": min(45, sensitive_raw),
            "cross_file": min(20, 4 * len(cross_file_targets)),
            "indirect_dynamic": min(15, 5 * indirect_calls),
            "ambiguous_resolution": min(10, 2 * uncertain_calls),
            "call_surface": min(10, len(calls)),
        }
        score = sum(components.values())

        scored.append(
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "priority_score": score,
                "score_components": components,
                "chunk_count": len(chunk_ids),
                "call_count": len(calls),
                "cross_file_target_count": len(cross_file_targets),
                "indirect_dynamic_call_count": indirect_calls,
                "ambiguous_resolution_count": uncertain_calls,
                "sensitive_api_categories": dict(sorted(categories.items())),
                "sensitive_api_names": dict(sorted(sensitive_names.items())),
            }
        )

    # Deterministic tie break. Every Batch stays in the queue.
    scored.sort(key=lambda r: (-int(r["priority_score"]), str(r["batch_id"])))
    for rank, record in enumerate(scored, 1):
        record["priority_rank"] = rank

    output = args.output if args.output.is_absolute() else root / args.output
    summary_path = args.summary if args.summary.is_absolute() else root / args.summary
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in scored:
            handle.write(canonical(record) + "\n")

    manifest_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    summary = {
        "schema_version": 1,
        "status": "PASS",
        "policy": "risk_aware_full_coverage_scheduler",
        "security_judgment": False,
        "all_batches_preserved": len(scored) == len(batches),
        "batch_count": len(scored),
        "priority_manifest_sha256": manifest_sha,
        "score_definition": {
            "sensitive_api": "generic security-relevant API call hints, capped at 45",
            "cross_file": "external definition relationships, 4 points each, capped at 20",
            "indirect_dynamic": "indirect/dynamic calls, 5 points each, capped at 15",
            "ambiguous_resolution": "ambiguous/qualified call resolution, 2 points each, capped at 10",
            "call_surface": "number of calls in Batch, capped at 10",
            "maximum_score": 100,
        },
        "top_batches": scored[: max(0, args.top)],
        "note": (
            "Priority score determines analysis order only. "
            "No Batch is skipped and no vulnerability is declared by the scheduler."
        ),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
