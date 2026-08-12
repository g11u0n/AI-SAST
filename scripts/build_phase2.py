#!/usr/bin/env python3
"""Build deterministic structural Chunk/Batch artifacts and freeze evaluation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tree_sitter  # noqa: E402

from src.chunking import StructuralParser, build_batches, build_file_chunks  # noqa: E402
from src.chunking.blob_source import GitBlobSource  # noqa: E402
from src.chunking.tokenizer import decode_source, rendered_slice  # noqa: E402


HASH_DOMAIN = b"ai-sast-experiment-semantic-v1\0"
EXPERIMENT_ID_PREFIX = "exp-v1-"
MAX_EVIDENCE_UTF8_BYTES = 8192
MAX_CHUNK_CONTENT_UTF8_BYTES = 3000
CANONICAL_CHUNKING_DIR = Path("artifacts/chunking")
CANONICAL_SELECTION = Path("artifacts/evaluation/selection.json")
CORE_ARTIFACTS = (
    "chunking_contract.json",
    "file_manifest.jsonl",
    "chunk_manifest.jsonl",
    "batch_manifest.jsonl",
    "coverage_report.json",
    "phase2_report.json",
)


class Phase2Error(RuntimeError):
    pass


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Phase2Error(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Phase2Error(f"Cannot read strict JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Phase2Error(f"Expected a JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise Phase2Error(f"JSONL must be UTF-8 without BOM and LF-only: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise Phase2Error(f"Blank JSONL line at {path}:{line_number}")
        try:
            value = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise Phase2Error(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise Phase2Error(f"JSONL record is not an object at {path}:{line_number}")
        records.append(value)
    return records


def canonical_json(value: Any, *, newline: bool = True) -> bytes:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return body + (b"\n" if newline else b"")


def pretty_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_jsonl(records: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json(record) for record in records)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def semantic_identity(semantic: dict[str, Any]) -> tuple[str, str]:
    digest = sha256(HASH_DOMAIN + canonical_json(semantic, newline=False))
    return digest, EXPERIMENT_ID_PREFIX + digest[:24]


def find_git(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    located = shutil.which("git")
    if located:
        candidates.append(Path(located))
    candidates.append(Path(r"C:\Program Files\Git\cmd\git.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise Phase2Error("Git executable was not found")


def validate_source_records(
    records: list[dict[str, Any]], target_lock: dict[str, Any]
) -> None:
    expected_count = target_lock["expected_inventory"]["in_scope_files"]
    if len(records) != expected_count:
        raise Phase2Error(f"Expected {expected_count} source records, got {len(records)}")
    paths: set[str] = set()
    oids: set[str] = set()
    allowed_extensions = set(
        target_lock["expected_inventory"]["in_scope_by_extension"]
    )
    for record in records:
        expected_keys = {
            "path",
            "git_blob_oid",
            "mode",
            "size_bytes",
            "extension",
            "classification",
            "reason",
        }
        if set(record) != expected_keys:
            raise Phase2Error(f"Unexpected source manifest keys for {record.get('path')}")
        path = record["path"]
        oid = record["git_blob_oid"]
        if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
            raise Phase2Error("Source manifest has an invalid relative path")
        if "\\" in path or ".." in Path(path).parts:
            raise Phase2Error(f"Non-canonical source path: {path}")
        if path in paths:
            raise Phase2Error(f"Duplicate source path: {path}")
        paths.add(path)
        if not isinstance(oid, str) or len(oid) != 40:
            raise Phase2Error(f"Invalid blob OID for {path}")
        if oid in oids:
            # Identical blobs at different paths are legal; only path uniqueness is required.
            pass
        oids.add(oid)
        if record["classification"] != "IN_SCOPE":
            raise Phase2Error(f"Non-scope record in source manifest: {path}")
        if record["extension"] not in allowed_extensions:
            raise Phase2Error(f"Unexpected extension in source manifest: {path}")
        if not isinstance(record["size_bytes"], int) or record["size_bytes"] < 0:
            raise Phase2Error(f"Invalid source size for {path}")


def grammar_fingerprint(language: Any) -> dict[str, Any]:
    kinds = []
    for node_id in range(language.node_kind_count):
        kinds.append(
            [
                node_id,
                language.node_kind_for_id(node_id),
                language.node_kind_is_named(node_id),
                language.node_kind_is_visible(node_id),
                language.node_kind_is_supertype(node_id),
            ]
        )
    semantic_version = getattr(language, "semantic_version", None)
    if isinstance(semantic_version, tuple):
        semantic_value: list[int] | None = list(semantic_version)
    else:
        semantic_value = None
    return {
        "abi_version": language.abi_version,
        "semantic_version": semantic_value,
        "node_kind_count": language.node_kind_count,
        "node_kind_fingerprint_sha256": sha256(canonical_json(kinds, newline=False)),
    }


def make_contract(
    *,
    target_lock: dict[str, Any],
    source_manifest_sha256: str,
    parser: StructuralParser,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "target": {
            "commit_sha": target_lock["target"]["commit_sha"],
            "scope_sha256": target_lock["analysis_scope"]["scope_sha256"],
            "file_list_sha256": target_lock["analysis_scope"]["target_file_list_sha256"],
            "in_scope_files": target_lock["expected_inventory"]["in_scope_files"],
        },
        "input": {
            "source_manifest_path": "artifacts/coverage/source_manifest.jsonl",
            "source_manifest_sha256": source_manifest_sha256,
            "content_source": "git_cat_file_blob_by_manifest_oid",
            "worktree_source_allowed": False,
        },
        "runtime": {
            "tree_sitter_version": importlib.metadata.version("tree-sitter"),
            "tree_sitter_c_version": importlib.metadata.version("tree-sitter-c"),
            "tree_sitter_cpp_version": importlib.metadata.version("tree-sitter-cpp"),
            "tree_sitter_language_version": tree_sitter.LANGUAGE_VERSION,
            "tree_sitter_min_compatible_language_version": tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION,
            "grammars": {
                "c": grammar_fingerprint(parser.languages["c"]),
                "cpp": grammar_fingerprint(parser.languages["cpp"]),
            },
        },
        "decode_policy": {
            "order": ["utf-8", "cp1252"],
            "errors": "strict",
            "replacement_allowed": False,
            "nul_allowed": False,
            "non_utf8_parse_mode": "deterministic_line_window_fallback",
        },
        "parser_policy": {
            "c_extension": "c",
            "cpp_extension": "cpp",
            "header_grammars": ["c", "cpp"],
            "header_score": [
                "error_byte_union",
                "missing_count",
                "error_count",
                "grammar_tiebreak_c_first",
            ],
            "syntax_error_mode": "hybrid_clean_structural_units_diagnostic_line_window",
            "preprocessor_policy": "raw_unexpanded_all_branches",
        },
        "chunking": {
            "algorithm": "transparent_containers_protected_units_recursive_ast_hybrid_fallback_v2",
            "coverage": "exact_non_overlapping_half_open_byte_partition",
            "max_rendered_content_utf8_bytes": MAX_CHUNK_CONTENT_UTF8_BYTES,
            "chunk_id_domain": "ai-sast-chunk-id-v1",
        },
        "batching": {
            "algorithm": "source_order_next_fit_v1",
            "renderer_version": "evidence-frame-v1",
            "max_payload_utf8_bytes": MAX_EVIDENCE_UTF8_BYTES,
            "budget_counter": "utf8_byte_upper_bound_v1",
            "budget_unit_rule": "one_budget_unit_per_utf8_byte",
            "batch_id_domain": "ai-sast-batch-id-v1",
        },
        "serialization": "utf8_no_bom_lf_sorted_keys_compact_json_allow_nan_false",
    }


def run_phase0_verifier(root: Path, repo: Path, git_executable: Path) -> None:
    command = [
        sys.executable,
        str(root / "scripts" / "verify_target_lock.py"),
        "--repo",
        str(repo),
        "--git",
        str(git_executable),
    ]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip()
        raise Phase2Error(f"Phase 0 prerequisite verification failed: {detail}")


def run_phase1_verifier(root: Path) -> None:
    command = [sys.executable, str(root / "scripts" / "verify_experiment_lock.py")]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip()
        raise Phase2Error(f"Phase 1 prerequisite verification failed: {detail}")


def validate_locked_batch_budget(experiment_lock: dict[str, Any]) -> None:
    semantic = experiment_lock["semantic"]
    locked_cap = semantic["context_envelope"]["component_caps"][
        "evidence_or_raw_batch_tokens"
    ]
    accounting = semantic["token_accounting"]
    if locked_cap != MAX_EVIDENCE_UTF8_BYTES:
        raise Phase2Error(
            "Phase 2 v1 Evidence cap differs from experiment.lock.yaml: "
            f"{locked_cap} != {MAX_EVIDENCE_UTF8_BYTES}"
        )
    if (
        accounting["batch_budget_counter"] != "utf8_byte_upper_bound_v1"
        or accounting["batch_budget_unit_rule"] != "one_budget_unit_per_utf8_byte"
    ):
        raise Phase2Error("Phase 2 Batch counter differs from experiment.lock.yaml")


def build_artifact_bytes(
    *, root: Path, repo: Path, git_executable: Path
) -> tuple[dict[str, bytes], bytes, dict[str, Any]]:
    target_lock = load_json(root / "target.lock.yaml")
    experiment_lock = load_json(root / "experiment.lock.yaml")
    validate_locked_batch_budget(experiment_lock)
    source_path = root / "artifacts" / "coverage" / "source_manifest.jsonl"
    source_bytes = source_path.read_bytes()
    source_records = load_jsonl(source_path)
    validate_source_records(source_records, target_lock)
    target_commit_sha = target_lock["target"]["commit_sha"]
    target_scope_sha256 = target_lock["analysis_scope"]["scope_sha256"]
    target_binding = experiment_lock["semantic"]["target_binding"]
    if target_binding != {
        "target_commit_sha": target_commit_sha,
        "target_scope_sha256": target_scope_sha256,
        "target_file_list_sha256": target_lock["analysis_scope"]["target_file_list_sha256"],
    }:
        raise Phase2Error("Experiment target binding differs from the Phase 0 lock")

    parser = StructuralParser(max_source_utf8_bytes=MAX_CHUNK_CONTENT_UTF8_BYTES)
    contract = make_contract(
        target_lock=target_lock,
        source_manifest_sha256=sha256(source_bytes),
        parser=parser,
    )
    contract_bytes = canonical_json(contract)
    contract_sha256 = sha256(contract_bytes)
    file_records: list[dict[str, Any]] = []
    chunk_records: list[dict[str, Any]] = []
    raw_by_path: dict[str, bytes] = {}
    terminal_errors: list[str] = []

    with GitBlobSource(git_executable=git_executable, repository=repo) as blob_source:
        for source_record in source_records:
            path = source_record["path"]
            try:
                raw = blob_source.read_blob(
                    source_record["git_blob_oid"],
                    expected_size=source_record["size_bytes"],
                )
                _, source_encoding = decode_source(raw)
                parsed = parser.parse(
                    raw,
                    extension=source_record["extension"],
                    source_encoding=source_encoding,
                )
                chunks = build_file_chunks(
                    source_record=source_record,
                    raw=raw,
                    parsed=parsed,
                    target_commit_sha=target_commit_sha,
                    target_scope_sha256=target_scope_sha256,
                )
                cursor = 0
                for chunk in chunks:
                    if chunk["start_byte"] != cursor:
                        raise Phase2Error(f"Coverage gap or overlap while building {path}")
                    cursor = chunk["end_byte_exclusive"]
                    if chunk["evidence_frame_utf8_bytes"] > MAX_EVIDENCE_UTF8_BYTES:
                        raise Phase2Error(f"Rendered Chunk exceeds Batch budget: {path}")
                if cursor != len(raw):
                    raise Phase2Error(f"Incomplete coverage while building {path}")
                raw_by_path[path] = raw
                chunk_records.extend(chunks)
                file_records.append(
                    {
                        "schema_version": 1,
                        "target_commit_sha": target_commit_sha,
                        "target_scope_sha256": target_scope_sha256,
                        "path": path,
                        "git_blob_oid": source_record["git_blob_oid"],
                        "extension": source_record["extension"],
                        "raw_size_bytes": len(raw),
                        "raw_content_sha256": sha256(raw),
                        "source_encoding": source_encoding,
                        "language": parsed.language,
                        "grammar": parsed.grammar,
                        "parse_outcome": parsed.outcome,
                        "selected_parse_score": (
                            None if parsed.selected_score is None else parsed.selected_score.as_list()
                        ),
                        "parser_attempts": list(parsed.parser_attempts),
                        "fallback_reason": parsed.fallback_reason,
                        "chunk_count": len(chunks),
                        "covered_bytes": cursor,
                        "unmapped_ranges": 0,
                        "overlap_ranges": 0,
                    }
                )
            except Exception as exc:
                terminal_errors.append(f"{path}:{type(exc).__name__}:{exc}")
    if terminal_errors:
        raise Phase2Error(
            "Terminal source processing errors prevent artifact publication: "
            + " | ".join(terminal_errors[:10])
        )

    file_records.sort(key=lambda item: item["path"].encode("utf-8"))
    chunk_records.sort(
        key=lambda item: (
            item["path"].encode("utf-8"),
            item["start_byte"],
            item["end_byte_exclusive"],
            item["chunk_id"],
        )
    )
    chunk_ids = [record["chunk_id"] for record in chunk_records]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise Phase2Error("Truncated Chunk ID collision")
    file_manifest_bytes = canonical_jsonl(file_records)
    chunk_manifest_bytes = canonical_jsonl(chunk_records)

    def content_loader(chunk: dict[str, Any]) -> str:
        raw = raw_by_path[chunk["path"]][
            chunk["start_byte"] : chunk["end_byte_exclusive"]
        ]
        return rendered_slice(raw, chunk["source_encoding"])

    batch_records = build_batches(
        chunks=chunk_records,
        content_loader=content_loader,
        target_commit_sha=target_commit_sha,
        target_scope_sha256=target_scope_sha256,
        source_manifest_sha256=sha256(source_bytes),
        chunking_contract_sha256=contract_sha256,
        file_manifest_sha256=sha256(file_manifest_bytes),
        chunk_manifest_sha256=sha256(chunk_manifest_bytes),
        max_payload_utf8_bytes=MAX_EVIDENCE_UTF8_BYTES,
    )
    if len(batch_records) < experiment_lock["semantic"]["evaluation_contract"]["batch_count"]:
        raise Phase2Error("Fewer than three Batches were generated")
    batch_manifest_bytes = canonical_jsonl(batch_records)
    batch_manifest_sha256 = sha256(batch_manifest_bytes)
    evaluation = experiment_lock["semantic"]["evaluation_contract"]
    domain = evaluation["selection_hash_domain"].encode("utf-8")
    seed = str(evaluation["selection_seed"]).encode("ascii")
    ranked_ids = sorted(
        [record["batch_id"] for record in batch_records],
        key=lambda batch_id: (
            sha256(domain + b"\0" + seed + b"\0" + batch_id.encode("utf-8")),
            batch_id,
        ),
    )
    selection = {
        "schema_version": 1,
        "target_commit_sha": target_commit_sha,
        "target_scope_sha256": target_scope_sha256,
        "batch_manifest_path": evaluation["batch_manifest_artifact"],
        "batch_manifest_sha256": batch_manifest_sha256,
        "selection_seed": evaluation["selection_seed"],
        "selection_seed_encoding": evaluation["selection_seed_encoding"],
        "selection_algorithm": evaluation["selection_algorithm"],
        "selection_hash_domain": evaluation["selection_hash_domain"],
        "batch_count": evaluation["batch_count"],
        "selected_batch_ids": ranked_ids[: evaluation["batch_count"]],
        "frozen_before_any_llm_result": True,
        "replace_after_result_observation": False,
    }
    selection_bytes = canonical_json(selection)
    outcome_counts = Counter(record["parse_outcome"] for record in file_records)
    kind_counts = Counter(record["kind"] for record in chunk_records)
    coverage = {
        "schema_version": 1,
        "target_commit_sha": target_commit_sha,
        "target_scope_sha256": target_scope_sha256,
        "source_file_count": len(source_records),
        "parse_success": outcome_counts["PARSE_SUCCESS"],
        "fallback_success": outcome_counts["FALLBACK_SUCCESS"],
        "parse_error": 0,
        "terminal_error": 0,
        "status_sum": sum(outcome_counts.values()),
        "chunk_count": len(chunk_records),
        "batch_count": len(batch_records),
        "mapped_source_bytes": sum(record["covered_bytes"] for record in file_records),
        "source_bytes": sum(record["raw_size_bytes"] for record in file_records),
        "unmapped_in_scope_ranges": 0,
        "overlap_in_scope_ranges": 0,
        "duplicate_chunk_ids": 0,
        "oversized_chunks": 0,
        "oversized_batches": 0,
        "chunk_kind_counts": dict(sorted(kind_counts.items())),
    }
    coverage_bytes = canonical_json(coverage)
    report = {
        "schema_version": 1,
        "status": "PENDING_REPRODUCIBILITY",
        "target_commit_sha": target_commit_sha,
        "target_scope_sha256": target_scope_sha256,
        "counts": coverage,
        "artifact_sha256": {
            "chunking_contract.json": contract_sha256,
            "file_manifest.jsonl": sha256(file_manifest_bytes),
            "chunk_manifest.jsonl": sha256(chunk_manifest_bytes),
            "batch_manifest.jsonl": batch_manifest_sha256,
            "coverage_report.json": sha256(coverage_bytes),
            "selection.json": sha256(selection_bytes),
        },
        "reproducibility": {
            "method": "two_independent_in_memory_builds_from_locked_git_blobs",
            "byte_identical": False,
            "compared_artifacts": [
                "chunking_contract.json",
                "file_manifest.jsonl",
                "chunk_manifest.jsonl",
                "batch_manifest.jsonl",
                "coverage_report.json",
                "selection.json",
            ],
        },
    }
    artifacts = {
        "chunking_contract.json": contract_bytes,
        "file_manifest.jsonl": file_manifest_bytes,
        "chunk_manifest.jsonl": chunk_manifest_bytes,
        "batch_manifest.jsonl": batch_manifest_bytes,
        "coverage_report.json": coverage_bytes,
        "phase2_report.json": canonical_json(report),
    }
    return artifacts, selection_bytes, report


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def publish_artifacts(
    output_dir: Path,
    selection_path: Path,
    artifacts: dict[str, bytes],
    selection_bytes: bytes,
) -> None:
    for name in CORE_ARTIFACTS:
        atomic_write(output_dir / name, artifacts[name])
    atomic_write(selection_path, selection_bytes)


def observed_result_files(root: Path) -> list[Path]:
    observed_roots = [
        root / "results",
        root / "artifacts" / "telemetry",
        root / "artifacts" / "evaluation" / "llm_results",
    ]
    return [
        path
        for directory in observed_roots
        if directory.exists()
        for path in directory.rglob("*")
        if path.is_file()
    ]


def prepare_experiment_freeze(
    *,
    root: Path,
    batch_manifest_bytes: bytes,
    selection_bytes: bytes,
    allow_pre_result_refreeze: bool,
) -> tuple[bytes, str, str]:
    lock_path = root / "experiment.lock.yaml"
    lock = load_json(lock_path)
    semantic = copy.deepcopy(lock["semantic"])
    binding = {
        "batch_manifest_path": "artifacts/chunking/batch_manifest.jsonl",
        "batch_manifest_sha256": sha256(batch_manifest_bytes),
        "selection_path": "artifacts/evaluation/selection.json",
        "selection_sha256": sha256(selection_bytes),
    }
    stage = semantic["lock_stage"]
    if stage == "evaluation_frozen":
        if semantic.get("evaluation_binding") != binding:
            if not allow_pre_result_refreeze:
                raise Phase2Error("Frozen evaluation artifacts cannot be replaced")
            if observed_result_files(root):
                raise Phase2Error("Frozen evaluation artifacts cannot change after results exist")
            old_binding = semantic["evaluation_binding"]
            old_batch = root / old_binding["batch_manifest_path"]
            old_selection = root / old_binding["selection_path"]
            if (
                not old_batch.is_file()
                or not old_selection.is_file()
                or sha256(old_batch.read_bytes()) != old_binding["batch_manifest_sha256"]
                or sha256(old_selection.read_bytes()) != old_binding["selection_sha256"]
            ):
                raise Phase2Error("Existing frozen artifacts are already inconsistent")
            semantic["evaluation_binding"] = binding
    elif stage == "preflight_locked":
        if observed_result_files(root):
            raise Phase2Error("Cannot freeze selection after LLM result artifacts exist")
        semantic["lock_stage"] = "evaluation_frozen"
        semantic["evaluation_binding"] = binding
    else:
        raise Phase2Error(f"Unsupported experiment lock stage: {stage}")
    semantic_sha256, experiment_id = semantic_identity(semantic)
    lock["semantic"] = semantic
    lock["identity"]["semantic_sha256"] = semantic_sha256
    lock["identity"]["experiment_id"] = experiment_id
    return pretty_json(lock), semantic_sha256, experiment_id


def compare_builds(
    first: tuple[dict[str, bytes], bytes, dict[str, Any]],
    second: tuple[dict[str, bytes], bytes, dict[str, Any]],
) -> None:
    first_artifacts, first_selection, _ = first
    second_artifacts, second_selection, _ = second
    for name in CORE_ARTIFACTS:
        if first_artifacts[name] != second_artifacts[name]:
            raise Phase2Error(f"Non-deterministic Phase 2 artifact: {name}")
    if first_selection != second_selection:
        raise Phase2Error("Non-deterministic Phase 2 selection artifact")


def mark_reproducibility_verified(
    build: tuple[dict[str, bytes], bytes, dict[str, Any]]
) -> None:
    artifacts, _, report = build
    report["status"] = "PASS"
    report["reproducibility"]["byte_identical"] = True
    artifacts["phase2_report.json"] = canonical_json(report)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--git", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--selection-output", type=Path)
    parser.add_argument("--freeze-experiment", action="store_true")
    parser.add_argument("--allow-pre-result-refreeze", action="store_true")
    parser.add_argument("--skip-repro-check", action="store_true")
    arguments = parser.parse_args()
    try:
        root = arguments.project_root.resolve()
        repo = (
            arguments.repo.resolve()
            if arguments.repo is not None
            else (root / ".target-src" / "userland").resolve()
        )
        git_executable = find_git(arguments.git)
        output_dir = (
            arguments.output_dir.resolve()
            if arguments.output_dir is not None
            else (root / CANONICAL_CHUNKING_DIR).resolve()
        )
        selection_path = (
            arguments.selection_output.resolve()
            if arguments.selection_output is not None
            else (
                (root / CANONICAL_SELECTION).resolve()
                if output_dir == (root / CANONICAL_CHUNKING_DIR).resolve()
                else (output_dir / "selection.json").resolve()
            )
        )
        if arguments.freeze_experiment and (
            output_dir != (root / CANONICAL_CHUNKING_DIR).resolve()
            or selection_path != (root / CANONICAL_SELECTION).resolve()
        ):
            raise Phase2Error("Experiment freeze requires canonical artifact paths")
        run_phase0_verifier(root, repo, git_executable)
        run_phase1_verifier(root)
        first = build_artifact_bytes(root=root, repo=repo, git_executable=git_executable)
        if not arguments.skip_repro_check:
            second = build_artifact_bytes(root=root, repo=repo, git_executable=git_executable)
            compare_builds(first, second)
            mark_reproducibility_verified(first)
        elif arguments.freeze_experiment:
            raise Phase2Error("Experiment freeze requires the two-build reproducibility check")
        artifacts, selection_bytes, report = first
        canonical_output = output_dir == (root / CANONICAL_CHUNKING_DIR).resolve()
        if canonical_output and arguments.skip_repro_check:
            raise Phase2Error("Canonical publication requires the reproducibility check")
        if canonical_output and not arguments.freeze_experiment:
            raise Phase2Error("Canonical publication requires atomic Experiment freeze preparation")
        identity: tuple[str, str] | None = None
        lock_bytes: bytes | None = None
        if arguments.freeze_experiment:
            lock_bytes, semantic_sha256, experiment_id = prepare_experiment_freeze(
                root=root,
                batch_manifest_bytes=artifacts["batch_manifest.jsonl"],
                selection_bytes=selection_bytes,
                allow_pre_result_refreeze=arguments.allow_pre_result_refreeze,
            )
            identity = (semantic_sha256, experiment_id)
        publish_artifacts(output_dir, selection_path, artifacts, selection_bytes)
        if lock_bytes is not None:
            atomic_write(root / "experiment.lock.yaml", lock_bytes)
        result = {
            "status": report["status"],
            "parse_success": report["counts"]["parse_success"],
            "fallback_success": report["counts"]["fallback_success"],
            "terminal_error": report["counts"]["terminal_error"],
            "chunk_count": report["counts"]["chunk_count"],
            "batch_count": report["counts"]["batch_count"],
            "selection": json.loads(selection_bytes)["selected_batch_ids"],
            "experiment_id": None if identity is None else identity[1],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (Phase2Error, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Phase 2 build FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
