#!/usr/bin/env python3
"""Build the deterministic, source-grounded Compact Code Index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.chunking.blob_source import GitBlobSource  # noqa: E402
from src.index import SymbolExtractor, canonical_json, canonical_jsonl  # noqa: E402
from src.index.code_index import sha256  # noqa: E402
from src.index.relation_index import (  # noqa: E402
    resolve_call_edges,
    resolve_include_edges,
)


CANONICAL_INDEX_DIR = Path("artifacts/index")
INDEX_BUNDLE_DOMAIN = b"ai-sast-code-index-v1\0"
ARTIFACT_NAMES = (
    "index_contract.json",
    "file_index.jsonl",
    "symbol_index.jsonl",
    "call_edges.jsonl",
    "include_edges.jsonl",
    "chunk_facts.jsonl",
    "selected_batch_map.json",
    "phase3_report.json",
)


class Phase3Error(RuntimeError):
    pass


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise Phase3Error(f"Duplicate JSON key: {key}")
        output[key] = value
    return output


def load_json(path: Path, *, canonical: bool = False) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")) or b"\r" in raw:
        raise Phase3Error(f"Non-canonical encoding or newline: {path}")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                Phase3Error(f"Non-finite JSON value: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase3Error(f"Invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise Phase3Error(f"JSON artifact must be an object: {path}")
    if canonical and raw != canonical_json(value):
        raise Phase3Error(f"JSON artifact is not canonical: {path}")
    return value


def load_jsonl(path: Path, *, canonical: bool = True) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")) or b"\r" in raw:
        raise Phase3Error(f"Non-canonical encoding or newline: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
        if not line.endswith(b"\n") or not line.strip():
            raise Phase3Error(f"Invalid JSONL framing at {path}:{line_number}")
        try:
            value = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    Phase3Error(f"Non-finite JSON value: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3Error(f"Invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise Phase3Error(f"JSONL record must be an object at {path}:{line_number}")
        if canonical and line != canonical_json(value):
            raise Phase3Error(f"Non-canonical JSONL at {path}:{line_number}")
        records.append(value)
    return records


def find_git(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.resolve()
        if candidate.is_file():
            return candidate
        raise Phase3Error(f"Git executable not found: {candidate}")
    discovered = shutil.which("git")
    if discovered:
        return Path(discovered).resolve()
    standard = Path(r"C:\Program Files\Git\cmd\git.exe")
    if standard.is_file():
        return standard
    raise Phase3Error("Git executable was not found")


def run_upstream_verifiers(
    *, root: Path, repository: Path, git_executable: Path
) -> None:
    commands = (
        [
            sys.executable,
            str(root / "scripts" / "verify_experiment_lock.py"),
        ],
        [
            sys.executable,
            str(root / "scripts" / "verify_phase2.py"),
            "--repo",
            str(repository),
            "--git",
            str(git_executable),
            "--skip-rebuild-check",
        ],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise Phase3Error(f"Upstream verifier failed: {detail}")


def _validate_inputs(
    *,
    root: Path,
    source_records: list[dict[str, Any]],
    file_records: list[dict[str, Any]],
    chunk_records: list[dict[str, Any]],
    batch_records: list[dict[str, Any]],
    selection: dict[str, Any],
    target_lock: dict[str, Any],
    experiment_lock: dict[str, Any],
) -> None:
    target = target_lock["target"]
    scope = target_lock["analysis_scope"]
    if len(source_records) != target_lock["expected_inventory"]["in_scope_files"]:
        raise Phase3Error("Source manifest count differs from Target Lock")
    source_paths = [record["path"] for record in source_records]
    if len(source_paths) != len(set(source_paths)):
        raise Phase3Error("Duplicate source path")
    if source_paths != sorted(source_paths, key=lambda value: value.encode("utf-8")):
        raise Phase3Error("Source manifest order is not canonical")
    if any(
        path.startswith("/")
        or "\\" in path
        or ".." in PurePosixPath(path).parts
        for path in source_paths
    ):
        raise Phase3Error("Non-canonical source path")
    file_paths = [record["path"] for record in file_records]
    if file_paths != source_paths:
        raise Phase3Error("Phase 2 file manifest does not match source paths")
    if any(record["target_commit_sha"] != target["commit_sha"] for record in file_records):
        raise Phase3Error("Phase 2 file target commit drift")
    if any(record["target_scope_sha256"] != scope["scope_sha256"] for record in file_records):
        raise Phase3Error("Phase 2 file target scope drift")
    source_by_path = {record["path"]: record for record in source_records}
    chunks_by_path: dict[str, list[dict[str, Any]]] = {path: [] for path in source_paths}
    chunk_ids: set[str] = set()
    for chunk in chunk_records:
        path = chunk["path"]
        if path not in chunks_by_path:
            raise Phase3Error(f"Chunk path is out of scope: {path}")
        if chunk["chunk_id"] in chunk_ids:
            raise Phase3Error(f"Duplicate Chunk ID: {chunk['chunk_id']}")
        chunk_ids.add(chunk["chunk_id"])
        if chunk["git_blob_oid"] != source_by_path[path]["git_blob_oid"]:
            raise Phase3Error(f"Chunk Git blob drift: {chunk['chunk_id']}")
        chunks_by_path[path].append(chunk)
    for path, chunks in chunks_by_path.items():
        expected_size = source_by_path[path]["size_bytes"]
        cursor = 0
        for ordinal, chunk in enumerate(chunks):
            if chunk["file_chunk_ordinal"] != ordinal or chunk["start_byte"] != cursor:
                raise Phase3Error(f"Chunk partition drift: {path}")
            cursor = chunk["end_byte_exclusive"]
        if expected_size and (not chunks or cursor != expected_size):
            raise Phase3Error(f"Chunk coverage drift: {path}")
    batch_ids: set[str] = set()
    batched_chunks: list[str] = []
    for batch in batch_records:
        if batch["batch_id"] in batch_ids:
            raise Phase3Error(f"Duplicate Batch ID: {batch['batch_id']}")
        batch_ids.add(batch["batch_id"])
        batched_chunks.extend(batch["chunk_ids"])
    if len(batched_chunks) != len(chunk_ids) or set(batched_chunks) != chunk_ids:
        raise Phase3Error("Phase 2 Batch membership does not cover each Chunk once")
    selected = selection["selected_batch_ids"]
    if len(selected) != 3 or len(set(selected)) != 3:
        raise Phase3Error("Evaluation selection must contain three unique Batches")
    if any(batch_id not in batch_ids for batch_id in selected):
        raise Phase3Error("Evaluation selection contains an unknown Batch")
    identity = experiment_lock["identity"]
    semantic = experiment_lock["semantic"]
    if semantic["lock_stage"] != "evaluation_frozen":
        raise Phase3Error("Experiment must be evaluation_frozen before Index creation")
    binding = semantic["evaluation_binding"]
    batch_raw = (root / binding["batch_manifest_path"]).read_bytes()
    selection_raw = (root / binding["selection_path"]).read_bytes()
    if sha256(batch_raw) != binding["batch_manifest_sha256"]:
        raise Phase3Error("Experiment Batch binding mismatch")
    if sha256(selection_raw) != binding["selection_sha256"]:
        raise Phase3Error("Experiment selection binding mismatch")
    if selection["target_commit_sha"] != target["commit_sha"]:
        raise Phase3Error("Selection target commit drift")
    if selection["target_scope_sha256"] != scope["scope_sha256"]:
        raise Phase3Error("Selection target scope drift")
    if not identity["experiment_id"].startswith("exp-v1-"):
        raise Phase3Error("Invalid Experiment ID")


def make_contract(
    *,
    root: Path,
    target_lock: dict[str, Any],
    experiment_lock: dict[str, Any],
    phase2_contract: dict[str, Any],
) -> dict[str, Any]:
    target = target_lock["target"]
    scope = target_lock["analysis_scope"]
    binding_paths = {
        "source_manifest": "artifacts/coverage/source_manifest.jsonl",
        "phase2_file_manifest": "artifacts/chunking/file_manifest.jsonl",
        "phase2_chunk_manifest": "artifacts/chunking/chunk_manifest.jsonl",
        "phase2_batch_manifest": "artifacts/chunking/batch_manifest.jsonl",
        "phase2_chunking_contract": "artifacts/chunking/chunking_contract.json",
        "evaluation_selection": "artifacts/evaluation/selection.json",
        "experiment_lock": "experiment.lock.yaml",
    }
    input_binding = {
        name: {
            "path": relative,
            "sha256": sha256((root / relative).read_bytes()),
        }
        for name, relative in binding_paths.items()
    }
    return {
        "schema_version": 1,
        "contract_version": "compact-code-index-v1",
        "target": {
            "commit_sha": target["commit_sha"],
            "scope_sha256": scope["scope_sha256"],
            "file_list_sha256": scope["target_file_list_sha256"],
            "in_scope_files": target_lock["expected_inventory"]["in_scope_files"],
        },
        "experiment": {
            "experiment_id": experiment_lock["identity"]["experiment_id"],
            "semantic_sha256": experiment_lock["identity"]["semantic_sha256"],
            "lock_stage": experiment_lock["semantic"]["lock_stage"],
        },
        "input_binding": input_binding,
        "runtime": phase2_contract["runtime"],
        "extraction": {
            "content_source": "git_cat_file_blob_by_manifest_oid",
            "worktree_source_allowed": False,
            "preprocessor_policy": "raw_unexpanded_all_branches_with_depth",
            "utf8_policy": "selected_phase2_grammar_clean_subtrees",
            "non_utf8_policy": "no_ast_facts_preserve_byte_grounding",
            "diagnostic_policy": "skip_nodes_with_error_or_missing_descendants",
            "supported_symbols": [
                "FUNCTION",
                "GLOBAL_VARIABLE",
                "TYPEDEF",
                "STRUCT",
                "UNION",
                "ENUM",
                "CLASS",
                "NAMESPACE",
                "MACRO",
            ],
            "symbol_roles": ["DEFINITION", "DECLARATION"],
            "signature_max_utf8_bytes": 512,
            "parameter_max_utf8_bytes": 192,
            "function_body_stored": False,
            "type_body_stored": False,
            "macro_replacement_stored": False,
            "basic_fact_node_types": {
                "array_accesses": ["subscript_expression"],
                "assignments": ["assignment_expression"],
                "calls": ["call_expression"],
                "casts": ["cast_expression"],
                "conditions": [
                    "conditional_expression",
                    "if_statement",
                    "switch_statement",
                ],
                "field_accesses": ["field_expression"],
                "gotos": ["goto_statement"],
                "loops": [
                    "do_statement",
                    "for_range_loop",
                    "for_statement",
                    "while_statement",
                ],
                "pointer_operations": ["pointer_expression"],
                "returns": ["return_statement"],
                "sizeof_expressions": ["sizeof_expression"],
                "updates": ["update_expression"],
            },
        },
        "resolution": {
            "basis": "syntactic_name_candidates_without_build_configuration",
            "call_statuses": [
                "UNIQUE_FILE_LOCAL_CANDIDATE",
                "UNIQUE_REPOSITORY_CANDIDATE",
                "AMBIGUOUS",
                "UNRESOLVED_EXTERNAL",
                "INDIRECT_OR_DYNAMIC",
                "MEMBER_OR_QUALIFIED",
                "MACRO_OR_FUNCTION_AMBIGUOUS",
            ],
            "called_by_storage": "derived_inverse_view_only",
            "include_statuses": [
                "UNIQUE_PATH_CANDIDATE",
                "AMBIGUOUS_PATH_CANDIDATES",
                "UNRESOLVED_WITHOUT_BUILD_CONFIG",
            ],
        },
        "identity": {
            "symbol_domain": "ai-sast-symbol-id-v1",
            "call_domain": "ai-sast-call-id-v1",
            "include_domain": "ai-sast-include-id-v1",
            "bundle_domain": "ai-sast-code-index-v1",
            "short_id_hex_length": 24,
        },
        "serialization": {
            "format": "utf8_no_bom_lf_sorted_keys_compact_json_allow_nan_false",
            "record_order": "path_utf8_start_byte_kind_full_identity",
            "python_hash_forbidden": True,
            "absolute_path_in_artifact_forbidden": True,
        },
        "evaluation": {
            "selection_is_input_only": True,
            "batch_reselection_allowed": False,
            "selected_batch_map_path": "artifacts/index/selected_batch_map.json",
            "run_receipt_must_bind_index_bundle": True,
        },
    }


def _symbol_sort(record: dict[str, Any]) -> tuple[bytes, int, str, str]:
    return (
        record["path"].encode("utf-8"),
        record["reference"]["extent"]["start_byte"],
        record["symbol_kind"],
        record["symbol_identity_sha256"],
    )


def _relation_sort(record: dict[str, Any]) -> tuple[bytes, int, str]:
    return (
        record["path"].encode("utf-8"),
        record["reference"]["extent"]["start_byte"],
        record.get("call_identity_sha256", record.get("include_identity_sha256", "")),
    )


def _selected_batch_map(
    *,
    contract_sha256: str,
    selection: dict[str, Any],
    selection_sha256: str,
    batch_records: list[dict[str, Any]],
    chunk_records: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    includes: list[dict[str, Any]],
    chunk_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    batch_by_id = {record["batch_id"]: record for record in batch_records}
    chunk_by_id = {record["chunk_id"]: record for record in chunk_records}
    facts_by_id = {record["chunk_id"]: record for record in chunk_facts}
    entries: list[dict[str, Any]] = []
    for rank, batch_id in enumerate(selection["selected_batch_ids"], 1):
        batch = batch_by_id[batch_id]
        ordered_chunk_ids = list(batch["chunk_ids"])
        selected_chunks = [chunk_by_id[chunk_id] for chunk_id in ordered_chunk_ids]
        selected_set = set(ordered_chunk_ids)
        anchor_symbols = sorted(
            {
                symbol["symbol_id"]
                for symbol in symbols
                if symbol["reference"]["anchor"]["chunk_id"] in selected_set
            }
        )
        enclosing_symbols: set[str] = set()
        for symbol in symbols:
            extent = symbol["reference"]["extent"]
            for chunk in selected_chunks:
                if (
                    symbol["path"] == chunk["path"]
                    and extent["start_byte"] < chunk["end_byte_exclusive"]
                    and extent["end_byte_exclusive"] > chunk["start_byte"]
                ):
                    enclosing_symbols.add(symbol["symbol_id"])
                    break
        call_ids = sorted(
            call["call_id"]
            for call in calls
            if call["reference"]["anchor"]["chunk_id"] in selected_set
        )
        include_ids = sorted(
            edge["include_id"]
            for edge in includes
            if edge["reference"]["anchor"]["chunk_id"] in selected_set
        )
        empty_chunks: list[str] = []
        for chunk_id in ordered_chunk_ids:
            fact = facts_by_id[chunk_id]
            if (
                not fact["defined_symbol_ids"]
                and not fact["outgoing_call_ids"]
                and sum(fact["node_counts"].values()) == 0
                and not any(
                    edge["reference"]["anchor"]["chunk_id"] == chunk_id
                    for edge in includes
                )
            ):
                empty_chunks.append(chunk_id)
        entries.append(
            {
                "rank": rank,
                "batch_id": batch_id,
                "chunk_ids": ordered_chunk_ids,
                "payload_utf8_bytes": batch["payload_utf8_bytes"],
                "anchor_symbol_ids": anchor_symbols,
                "enclosing_symbol_ids": sorted(enclosing_symbols),
                "call_ids": call_ids,
                "include_ids": include_ids,
                "chunks_without_supported_facts": empty_chunks,
            }
        )
    return {
        "schema_version": 1,
        "index_contract_sha256": contract_sha256,
        "selection_sha256": selection_sha256,
        "selection_seed": selection["selection_seed"],
        "selected_batch_ids": list(selection["selected_batch_ids"]),
        "selection_order_preserved": True,
        "batch_reselection_performed": False,
        "batches": entries,
    }


def build_artifact_bytes(
    *, root: Path, repository: Path, git_executable: Path
) -> dict[str, bytes]:
    target_lock = load_json(root / "target.lock.yaml")
    experiment_lock = load_json(root / "experiment.lock.yaml")
    phase2_contract = load_json(
        root / "artifacts" / "chunking" / "chunking_contract.json",
        canonical=True,
    )
    source_path = root / "artifacts" / "coverage" / "source_manifest.jsonl"
    file_path = root / "artifacts" / "chunking" / "file_manifest.jsonl"
    chunk_path = root / "artifacts" / "chunking" / "chunk_manifest.jsonl"
    batch_path = root / "artifacts" / "chunking" / "batch_manifest.jsonl"
    selection_path = root / "artifacts" / "evaluation" / "selection.json"
    source_records = load_jsonl(source_path, canonical=False)
    file_records = load_jsonl(file_path, canonical=False)
    chunk_records = load_jsonl(chunk_path, canonical=False)
    batch_records = load_jsonl(batch_path, canonical=False)
    selection = load_json(selection_path, canonical=True)
    _validate_inputs(
        root=root,
        source_records=source_records,
        file_records=file_records,
        chunk_records=chunk_records,
        batch_records=batch_records,
        selection=selection,
        target_lock=target_lock,
        experiment_lock=experiment_lock,
    )
    contract = make_contract(
        root=root,
        target_lock=target_lock,
        experiment_lock=experiment_lock,
        phase2_contract=phase2_contract,
    )
    contract_bytes = canonical_json(contract)
    contract_sha = sha256(contract_bytes)
    source_by_path = {record["path"]: record for record in source_records}
    file_by_path = {record["path"]: record for record in file_records}
    chunks_by_path: dict[str, list[dict[str, Any]]] = {
        record["path"]: [] for record in source_records
    }
    for chunk in chunk_records:
        chunks_by_path[chunk["path"]].append(chunk)

    target_commit = target_lock["target"]["commit_sha"]
    target_scope = target_lock["analysis_scope"]["scope_sha256"]
    extractor = SymbolExtractor()
    output_files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    calls_pending: list[dict[str, Any]] = []
    includes_pending: list[dict[str, Any]] = []
    chunk_facts: list[dict[str, Any]] = []
    with GitBlobSource(
        git_executable=git_executable,
        repository=repository,
    ) as blob_source:
        for source in source_records:
            path = source["path"]
            raw = blob_source.read_blob(
                source["git_blob_oid"],
                expected_size=source["size_bytes"],
            )
            if sha256(raw) != file_by_path[path]["raw_content_sha256"]:
                raise Phase3Error(f"Git blob SHA-256 differs from Phase 2: {path}")
            extraction = extractor.extract(
                raw=raw,
                source_record=source,
                phase2_file=file_by_path[path],
                chunks=tuple(chunks_by_path[path]),
                target_commit_sha=target_commit,
                target_scope_sha256=target_scope,
                index_contract_sha256=contract_sha,
            )
            output_files.append(extraction.file_record)
            symbols.extend(extraction.symbols)
            calls_pending.extend(extraction.call_occurrences)
            includes_pending.extend(extraction.include_edges)
            chunk_facts.extend(extraction.chunk_facts)

    symbols.sort(key=_symbol_sort)
    symbol_ids = [record["symbol_id"] for record in symbols]
    if len(symbol_ids) != len(set(symbol_ids)):
        raise Phase3Error("Truncated Symbol ID collision")
    calls = resolve_call_edges(calls_pending, symbols)
    includes = resolve_include_edges(includes_pending, source_by_path)
    call_ids = [record["call_id"] for record in calls]
    include_ids = [record["include_id"] for record in includes]
    if len(call_ids) != len(set(call_ids)):
        raise Phase3Error("Truncated call ID collision")
    if len(include_ids) != len(set(include_ids)):
        raise Phase3Error("Truncated include ID collision")
    output_files.sort(key=lambda record: record["path"].encode("utf-8"))
    calls.sort(key=_relation_sort)
    includes.sort(key=_relation_sort)
    facts_by_id = {record["chunk_id"]: record for record in chunk_facts}
    if len(facts_by_id) != len(chunk_records):
        raise Phase3Error("Each Phase 2 Chunk must have one fact record")
    chunk_facts = [facts_by_id[chunk["chunk_id"]] for chunk in chunk_records]

    selection_sha = sha256(selection_path.read_bytes())
    selected_map = _selected_batch_map(
        contract_sha256=contract_sha,
        selection=selection,
        selection_sha256=selection_sha,
        batch_records=batch_records,
        chunk_records=chunk_records,
        symbols=symbols,
        calls=calls,
        includes=includes,
        chunk_facts=chunk_facts,
    )
    data_artifacts = {
        "index_contract.json": contract_bytes,
        "file_index.jsonl": canonical_jsonl(output_files),
        "symbol_index.jsonl": canonical_jsonl(symbols),
        "call_edges.jsonl": canonical_jsonl(calls),
        "include_edges.jsonl": canonical_jsonl(includes),
        "chunk_facts.jsonl": canonical_jsonl(chunk_facts),
        "selected_batch_map.json": canonical_json(selected_map),
    }
    artifact_hashes = {
        name: sha256(raw) for name, raw in sorted(data_artifacts.items())
    }
    bundle_identity = sha256(
        INDEX_BUNDLE_DOMAIN
        + canonical_json(artifact_hashes, newline=False)
    )
    statuses: dict[str, int] = {}
    for record in output_files:
        statuses[record["index_status"]] = statuses.get(record["index_status"], 0) + 1
    resolution_counts: dict[str, int] = {}
    for record in calls:
        resolution_counts[record["resolution"]] = (
            resolution_counts.get(record["resolution"], 0) + 1
        )
    report = {
        "schema_version": 1,
        "status": "PASS",
        "target_commit_sha": target_commit,
        "target_scope_sha256": target_scope,
        "experiment_id": experiment_lock["identity"]["experiment_id"],
        "index_bundle_id": f"I1-{bundle_identity[:24]}",
        "index_bundle_sha256": bundle_identity,
        "counts": {
            "files": len(output_files),
            "file_statuses": statuses,
            "symbols": len(symbols),
            "calls": len(calls),
            "includes": len(includes),
            "chunk_facts": len(chunk_facts),
            "call_resolutions": resolution_counts,
            "terminal_errors": 0,
        },
        "artifact_sha256": artifact_hashes,
        "source_bytes": sum(record["size_bytes"] for record in source_records),
        "index_data_bytes": sum(len(raw) for raw in data_artifacts.values()),
        "raw_source_or_security_judgment_stored": False,
        "reproducibility": {
            "status": "PENDING",
            "builds_compared": 0,
            "byte_identical": False,
        },
    }
    return {**data_artifacts, "phase3_report.json": canonical_json(report)}


def mark_reproducible(artifacts: dict[str, bytes]) -> dict[str, bytes]:
    output = dict(artifacts)
    report = json.loads(output["phase3_report.json"].decode("utf-8"))
    report["reproducibility"] = {
        "status": "VERIFIED",
        "builds_compared": 2,
        "byte_identical": True,
    }
    output["phase3_report.json"] = canonical_json(report)
    return output


def atomic_publish(directory: Path, artifacts: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACT_NAMES:
        destination = directory / name
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_bytes(artifacts[name])
        os.replace(temporary, destination)


def observed_results(root: Path) -> list[Path]:
    results = root / "results"
    if not results.exists():
        return []
    return sorted(
        (
            path
            for path in results.rglob("*")
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
        ),
        key=lambda path: path.as_posix().encode("utf-8"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="Project root containing locked artifacts",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(".target-src/userland"),
        help="Locked Raspberry Pi Userland checkout",
    )
    parser.add_argument("--git", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=CANONICAL_INDEX_DIR,
    )
    parser.add_argument("--skip-repro-check", action="store_true")
    parser.add_argument("--skip-upstream-verifiers", action="store_true")
    arguments = parser.parse_args()
    try:
        root = arguments.root.resolve()
        repository = (
            arguments.repo
            if arguments.repo.is_absolute()
            else root / arguments.repo
        ).resolve()
        output_dir = (
            arguments.output_dir
            if arguments.output_dir.is_absolute()
            else root / arguments.output_dir
        ).resolve()
        canonical_output = output_dir == (root / CANONICAL_INDEX_DIR).resolve()
        if canonical_output and arguments.skip_repro_check:
            raise Phase3Error("Canonical Index publication requires reproducibility check")
        if canonical_output and arguments.skip_upstream_verifiers:
            raise Phase3Error("Canonical Index publication requires upstream verifiers")
        git_executable = find_git(arguments.git)
        if not arguments.skip_upstream_verifiers:
            run_upstream_verifiers(
                root=root,
                repository=repository,
                git_executable=git_executable,
            )
        first = build_artifact_bytes(
            root=root,
            repository=repository,
            git_executable=git_executable,
        )
        if arguments.skip_repro_check:
            artifacts = first
        else:
            second = build_artifact_bytes(
                root=root,
                repository=repository,
                git_executable=git_executable,
            )
            if first != second:
                mismatches = [
                    name for name in ARTIFACT_NAMES if first.get(name) != second.get(name)
                ]
                raise Phase3Error(
                    "Phase 3 builds are not byte-identical: " + ", ".join(mismatches)
                )
            artifacts = mark_reproducible(first)
        if canonical_output and output_dir.exists():
            changed = any(
                (output_dir / name).exists()
                and (output_dir / name).read_bytes() != artifacts[name]
                for name in ARTIFACT_NAMES
            )
            if changed:
                results = observed_results(root)
                if results:
                    raise Phase3Error(
                        "Index changed after result observation: "
                        + ", ".join(path.relative_to(root).as_posix() for path in results[:5])
                    )
        atomic_publish(output_dir, artifacts)
        report = json.loads(artifacts["phase3_report.json"].decode("utf-8"))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, KeyError, TypeError, ValueError, Phase3Error) as exc:
        print(f"Phase 3 build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
