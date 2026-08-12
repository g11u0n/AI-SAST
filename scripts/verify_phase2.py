#!/usr/bin/env python3
"""Independent verifier for Phase 2 coverage, IDs, budgets, and selection."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import tree_sitter
import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_PAYLOAD = 8192
CHUNK_DOMAIN = b"ai-sast-chunk-id-v1\0"
BATCH_DOMAIN = b"ai-sast-batch-id-v1\0"
EXPERIMENT_DOMAIN = b"ai-sast-experiment-semantic-v1\0"
PHASE2_NAMES = (
    "chunking_contract.json",
    "file_manifest.jsonl",
    "chunk_manifest.jsonl",
    "batch_manifest.jsonl",
    "coverage_report.json",
    "phase2_report.json",
)

STRUCTURAL_KIND_BY_NODE = {
    "function_definition": "FUNCTION",
    "type_definition": "TYPE",
    "struct_specifier": "TYPE",
    "union_specifier": "TYPE",
    "enum_specifier": "TYPE",
    "class_specifier": "TYPE",
    "preproc_def": "MACRO",
    "preproc_function_def": "MACRO",
    "preproc_include": "PREPROCESSOR",
    "preproc_call": "PREPROCESSOR",
    "declaration": "DECLARATION",
    "template_declaration": "TEMPLATE",
}
TRANSPARENT_NODE_TYPES = {
    "translation_unit", "preproc_if", "preproc_ifdef", "preproc_ifndef",
    "preproc_elif", "preproc_else", "namespace_definition", "declaration_list",
    "linkage_specification",
}
PROTECTED_KINDS = {"FUNCTION", "TYPE", "MACRO", "DECLARATION", "TEMPLATE"}


class VerificationError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise VerificationError(message)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: Any, *, newline: bool = True) -> bytes:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        fail(f"Value cannot be canonicalized: {exc}")
    return body + (b"\n" if newline else b"")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path, *, require_canonical: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        fail(f"Cannot read {path}: {exc}")
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        fail(f"Artifact must be UTF-8 without BOM and LF-only: {path}")
    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Invalid strict JSON {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"Expected JSON object: {path}")
    if require_canonical and raw != canonical_json(value):
        fail(f"Artifact is not canonical JSON: {path}")
    return value


def load_jsonl(
    path: Path, *, require_canonical: bool = True
) -> tuple[list[dict[str, Any]], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        fail(f"Cannot read {path}: {exc}")
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n"):
        fail(f"JSONL must be UTF-8 without BOM, LF-only, and newline-terminated: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            fail(f"Blank JSONL line at {path}:{line_number}")
        try:
            value = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            fail(f"Invalid JSONL at {path}:{line_number}: {exc}")
        if not isinstance(value, dict):
            fail(f"JSONL record must be an object at {path}:{line_number}")
        if require_canonical and line + b"\n" != canonical_json(value):
            fail(f"Non-canonical JSONL record at {path}:{line_number}")
        records.append(value)
    return records, raw


def type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def exact_json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return type(left) is type(right) and left == right
    return left == right


def validate_schema(instance: Any, schema: dict[str, Any], location: str = "$") -> None:
    supported = {
        "$schema", "title", "type", "additionalProperties", "required", "properties",
        "const", "enum", "pattern", "minimum", "maximum", "minItems", "maxItems",
        "items", "minLength", "maxLength",
    }
    extras = set(schema) - supported
    if extras:
        fail(f"Unsupported JSON Schema keyword at {location}: {sorted(extras)}")
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(type_matches(instance, item) for item in expected):
            fail(f"Schema type violation at {location}")
    elif isinstance(expected, str) and not type_matches(instance, expected):
        fail(f"Schema type violation at {location}: expected {expected}")
    if "const" in schema and not exact_json_equal(instance, schema["const"]):
        fail(f"Schema const violation at {location}")
    if "enum" in schema and instance not in schema["enum"]:
        fail(f"Schema enum violation at {location}")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(instance)
        if missing:
            fail(f"Schema missing properties at {location}: {sorted(missing)}")
        if schema.get("additionalProperties") is False:
            extra_keys = set(instance) - set(properties)
            if extra_keys:
                fail(f"Schema extra properties at {location}: {sorted(extra_keys)}")
        for key, value in instance.items():
            child = properties.get(key)
            if isinstance(child, dict):
                validate_schema(value, child, f"{location}.{key}")
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            fail(f"Schema minItems violation at {location}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            fail(f"Schema maxItems violation at {location}")
        child = schema.get("items")
        if isinstance(child, dict):
            for index, value in enumerate(instance):
                validate_schema(value, child, f"{location}[{index}]")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            fail(f"Schema minLength violation at {location}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            fail(f"Schema maxLength violation at {location}")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            fail(f"Schema pattern violation at {location}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            fail(f"Schema minimum violation at {location}")
        if "maximum" in schema and instance > schema["maximum"]:
            fail(f"Schema maximum violation at {location}")


def find_git(explicit: Path | None) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    located = shutil.which("git")
    if located:
        candidates.append(Path(located))
    candidates.append(Path(r"C:\Program Files\Git\cmd\git.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    fail("Git executable not found")


class IndependentBlobReader:
    def __init__(self, git_executable: Path, repository: Path) -> None:
        self.process = subprocess.Popen(
            [str(git_executable), "-C", str(repository), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()

    def read(self, oid: str, expected_size: int) -> bytes:
        stdin = self.process.stdin
        stdout = self.process.stdout
        if stdin is None or stdout is None:
            fail("Git cat-file pipe is unavailable")
        stdin.write(oid.encode("ascii") + b"\n")
        stdin.flush()
        header = stdout.readline().rstrip(b"\n").split(b" ")
        if len(header) != 3:
            fail(f"Invalid git cat-file response for {oid}")
        actual, kind, size_value = header
        if actual.decode("ascii") != oid or kind != b"blob":
            fail(f"Manifest OID is not the expected Git blob: {oid}")
        size = int(size_value)
        raw = stdout.read(size)
        if stdout.read(1) != b"\n" or len(raw) != size:
            fail(f"Truncated Git blob response: {oid}")
        if size != expected_size:
            fail(f"Git blob size mismatch: {oid}")
        git_hash = hashlib.sha1(f"blob {size}\0".encode("ascii") + raw).hexdigest()  # noqa: S324
        if git_hash != oid:
            fail(f"Git blob content hash mismatch: {oid}")
        return raw


def git_tree_map(git_executable: Path, repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    completed = subprocess.run(
        [str(git_executable), "-C", str(repo), "ls-tree", "-r", "-z", commit],
        capture_output=True,
    )
    if completed.returncode != 0:
        fail(f"Cannot enumerate locked Git tree: {completed.stderr.decode('utf-8', 'replace')}")
    result: dict[str, tuple[str, str]] = {}
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, path_raw = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        path = path_raw.decode("utf-8", errors="strict")
        if kind == "blob":
            result[path] = (mode, oid)
    return result


def line_column(raw: bytes, offset: int) -> tuple[int, int]:
    line = raw.count(b"\n", 0, offset) + 1
    previous = raw.rfind(b"\n", 0, offset)
    return line, offset if previous < 0 else offset - previous - 1


def decode(raw: bytes) -> tuple[str, str]:
    if b"\x00" in raw:
        fail("NUL byte found in source blob")
    try:
        return raw.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252", errors="strict"), "cp1252"
        except UnicodeDecodeError:
            fail("Source blob matches neither locked strict decoder")


def render_frame(chunk: dict[str, Any], content: str) -> bytes:
    header = {
        "chunk_id": chunk["chunk_id"],
        "content_sha256": chunk["raw_content_sha256"],
        "end_line": chunk["end_line"],
        "kind": chunk["kind"],
        "path": chunk["path"],
        "start_line": chunk["start_line"],
    }
    return (
        b"@@AI_SAST_CHUNK_V1 "
        + canonical_json(header, newline=False)
        + b"\n<<<CODE\n"
        + content.encode("utf-8")
        + b"\nCODE>>>\n"
    )


def validate_chunk_semantics(chunk: dict[str, Any]) -> None:
    allowed_kinds = {
        "FUNCTION", "TYPE", "MACRO", "DECLARATION", "PREPROCESSOR",
        "NAMESPACE", "TEMPLATE", "LINKAGE", "AST_BLOCK", "LINE_WINDOW",
        "STRUCTURAL_GROUP",
    }
    if chunk["kind"] not in allowed_kinds:
        fail(f"Unknown Chunk kind: {chunk['chunk_id']}")
    if chunk["kind"] == "MACRO" and chunk["node_type"] not in {
        "preproc_def", "preproc_function_def"
    }:
        fail(f"MACRO Chunk node-type mismatch: {chunk['chunk_id']}")
    if chunk["kind"] == "FUNCTION" and chunk["node_type"] != "function_definition":
        fail(f"FUNCTION Chunk node-type mismatch: {chunk['chunk_id']}")
    if chunk["kind"] == "LINE_WINDOW" and chunk["parse_mode"] != "FALLBACK_SUCCESS":
        fail(f"LINE_WINDOW must belong to fallback parsing: {chunk['chunk_id']}")
    if chunk["kind"] == "STRUCTURAL_GROUP" and set(chunk["structural_kinds"]) & {
        "FUNCTION", "TYPE", "MACRO", "DECLARATION", "TEMPLATE", "LINE_WINDOW"
    }:
        fail(f"Protected structural unit was coalesced: {chunk['chunk_id']}")


def grammar_fingerprint(language: Language) -> dict[str, Any]:
    kinds = [
        [
            node_id,
            language.node_kind_for_id(node_id),
            language.node_kind_is_named(node_id),
            language.node_kind_is_visible(node_id),
            language.node_kind_is_supertype(node_id),
        ]
        for node_id in range(language.node_kind_count)
    ]
    semantic_version = getattr(language, "semantic_version", None)
    return {
        "abi_version": language.abi_version,
        "semantic_version": (
            list(semantic_version) if isinstance(semantic_version, tuple) else None
        ),
        "node_kind_count": language.node_kind_count,
        "node_kind_fingerprint_sha256": sha256(
            canonical_json(kinds, newline=False)
        ),
    }


def walk_nodes(root: Node) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def independent_parse_score(root: Node, grammar_tiebreak: int) -> list[int]:
    ranges: list[tuple[int, int]] = []
    missing = 0
    errors = 0
    for node in walk_nodes(root):
        if node.is_error:
            errors += 1
            if node.end_byte > node.start_byte:
                ranges.append((node.start_byte, node.end_byte))
        if node.is_missing:
            missing += 1
    union = 0
    cursor = -1
    for start, end in sorted(ranges):
        if end <= cursor:
            continue
        union += end - start if start >= cursor else end - cursor
        cursor = max(cursor, end)
    return [union, missing, errors, grammar_tiebreak]


def independent_kind(node_type: str) -> str:
    if node_type in STRUCTURAL_KIND_BY_NODE:
        return STRUCTURAL_KIND_BY_NODE[node_type]
    if node_type.startswith("preproc_"):
        return "PREPROCESSOR"
    return "AST_BLOCK"


def independent_transparent_units(
    node: Node, outer_start: int, outer_end: int
) -> list[tuple[int, int, Node]]:
    if node.type not in TRANSPARENT_NODE_TYPES:
        return [(outer_start, outer_end, node)]
    children = [
        child
        for child in node.named_children
        if child.end_byte > child.start_byte
        and child.start_byte >= outer_start
        and child.end_byte <= outer_end
    ]
    if not children:
        return [(outer_start, outer_end, node)]
    pieces: list[tuple[int, int, Node]] = []
    cursor = outer_start
    for child in children:
        if child.start_byte < cursor:
            continue
        pieces.extend(independent_transparent_units(child, cursor, child.end_byte))
        cursor = child.end_byte
    if cursor < outer_end and pieces:
        start, _, last = pieces[-1]
        pieces[-1] = (start, outer_end, last)
    return pieces


def independently_parse_file(
    raw: bytes,
    extension: str,
    parsers: dict[str, Parser],
) -> tuple[str, Node, list[int], list[dict[str, Any]]]:
    candidates = ["cpp"] if extension == ".cpp" else ["c"]
    if extension == ".h":
        candidates = ["c", "cpp"]
    parsed: list[tuple[str, Any, list[int]]] = []
    attempts: list[dict[str, Any]] = []
    for tie, language in enumerate(candidates):
        tree = parsers[language].parse(raw, encoding="utf8")
        score = independent_parse_score(tree.root_node, tie)
        parsed.append((language, tree, score))
        attempts.append({"grammar": language, "status": "OK", "score": score})
    language, tree, score = min(parsed, key=lambda item: tuple(item[2]))
    return language, tree.root_node, score, attempts


def validate_contract(
    contract: dict[str, Any], target: dict[str, Any], source_manifest_raw: bytes
) -> None:
    expected_keys = {
        "schema_version", "target", "input", "runtime", "decode_policy",
        "parser_policy", "chunking", "batching", "serialization",
    }
    if set(contract) != expected_keys or contract["schema_version"] != 1:
        fail("Unexpected Phase 2 contract shape")
    scope = target["analysis_scope"]
    inventory = target["expected_inventory"]
    if contract["target"] != {
        "commit_sha": target["target"]["commit_sha"],
        "scope_sha256": scope["scope_sha256"],
        "file_list_sha256": scope["target_file_list_sha256"],
        "in_scope_files": inventory["in_scope_files"],
    }:
        fail("Phase 2 contract target binding drift")
    if contract["input"] != {
        "source_manifest_path": "artifacts/coverage/source_manifest.jsonl",
        "source_manifest_sha256": sha256(source_manifest_raw),
        "content_source": "git_cat_file_blob_by_manifest_oid",
        "worktree_source_allowed": False,
    }:
        fail("Phase 2 input contract drift")
    if contract["decode_policy"] != {
        "order": ["utf-8", "cp1252"],
        "errors": "strict",
        "replacement_allowed": False,
        "nul_allowed": False,
        "non_utf8_parse_mode": "deterministic_line_window_fallback",
    }:
        fail("Phase 2 decoder contract drift")
    batching = contract["batching"]
    if batching != {
        "algorithm": "source_order_next_fit_v1",
        "renderer_version": "evidence-frame-v1",
        "max_payload_utf8_bytes": 8192,
        "budget_counter": "utf8_byte_upper_bound_v1",
        "budget_unit_rule": "one_budget_unit_per_utf8_byte",
        "batch_id_domain": "ai-sast-batch-id-v1",
    }:
        fail("Phase 2 Batch contract drift")
    if contract["chunking"]["max_rendered_content_utf8_bytes"] != 3000:
        fail("Phase 2 Chunk source budget drift")
    runtime = contract["runtime"]
    expected_runtime = {
        "tree_sitter_version": importlib.metadata.version("tree-sitter"),
        "tree_sitter_c_version": importlib.metadata.version("tree-sitter-c"),
        "tree_sitter_cpp_version": importlib.metadata.version("tree-sitter-cpp"),
        "tree_sitter_language_version": tree_sitter.LANGUAGE_VERSION,
        "tree_sitter_min_compatible_language_version": tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION,
        "grammars": {
            "c": grammar_fingerprint(Language(tree_sitter_c.language())),
            "cpp": grammar_fingerprint(Language(tree_sitter_cpp.language())),
        },
    }
    if runtime != expected_runtime:
        fail("Installed parser runtime or grammar fingerprint differs from contract")
    if contract["parser_policy"] != {
        "c_extension": "c",
        "cpp_extension": "cpp",
        "header_grammars": ["c", "cpp"],
        "header_score": [
            "error_byte_union", "missing_count", "error_count",
            "grammar_tiebreak_c_first",
        ],
        "syntax_error_mode": "hybrid_clean_structural_units_diagnostic_line_window",
        "preprocessor_policy": "raw_unexpanded_all_branches",
    }:
        fail("Phase 2 parser policy drift")
    if contract["chunking"] != {
        "algorithm": "transparent_containers_protected_units_recursive_ast_hybrid_fallback_v2",
        "coverage": "exact_non_overlapping_half_open_byte_partition",
        "max_rendered_content_utf8_bytes": 3000,
        "chunk_id_domain": "ai-sast-chunk-id-v1",
    }:
        fail("Phase 2 Chunking policy drift")
    if contract["serialization"] != "utf8_no_bom_lf_sorted_keys_compact_json_allow_nan_false":
        fail("Phase 2 serialization policy drift")


def verify(
    *,
    root: Path,
    repo: Path,
    git_executable: Path,
    chunking_dir: Path | None = None,
    selection_path: Path | None = None,
    check_lock: bool = True,
    rebuild_check: bool = False,
) -> dict[str, Any]:
    chunking_dir = chunking_dir or root / "artifacts" / "chunking"
    selection_path = selection_path or root / "artifacts" / "evaluation" / "selection.json"
    target = load_json(root / "target.lock.yaml")
    experiment = load_json(root / "experiment.lock.yaml")
    locked_cap = experiment["semantic"]["context_envelope"]["component_caps"][
        "evidence_or_raw_batch_tokens"
    ]
    accounting = experiment["semantic"]["token_accounting"]
    if locked_cap != MAX_PAYLOAD:
        fail("Phase 2 Evidence cap differs from experiment.lock.yaml")
    if (
        accounting["batch_budget_counter"] != "utf8_byte_upper_bound_v1"
        or accounting["batch_budget_unit_rule"] != "one_budget_unit_per_utf8_byte"
    ):
        fail("Phase 2 Batch counter differs from experiment.lock.yaml")
    source_records, source_raw = load_jsonl(
        root / "artifacts" / "coverage" / "source_manifest.jsonl",
        require_canonical=False,
    )
    contract = load_json(chunking_dir / "chunking_contract.json", require_canonical=True)
    file_records, file_raw = load_jsonl(chunking_dir / "file_manifest.jsonl")
    chunk_records, chunk_raw = load_jsonl(chunking_dir / "chunk_manifest.jsonl")
    batch_records, batch_raw = load_jsonl(chunking_dir / "batch_manifest.jsonl")
    coverage = load_json(chunking_dir / "coverage_report.json", require_canonical=True)
    report = load_json(chunking_dir / "phase2_report.json", require_canonical=True)
    selection = load_json(selection_path, require_canonical=True)
    validate_contract(contract, target, source_raw)

    file_schema = load_json(root / "schemas" / "phase2-file-record.schema.json")
    chunk_schema = load_json(root / "schemas" / "phase2-chunk-record.schema.json")
    batch_schema = load_json(root / "schemas" / "phase2-batch-record.schema.json")
    for index, record in enumerate(file_records):
        validate_schema(record, file_schema, f"$.files[{index}]")
    for index, record in enumerate(chunk_records):
        validate_schema(record, chunk_schema, f"$.chunks[{index}]")
    for index, record in enumerate(batch_records):
        validate_schema(record, batch_schema, f"$.batches[{index}]")

    expected_count = target["expected_inventory"]["in_scope_files"]
    if len(source_records) != expected_count or len(file_records) != expected_count:
        fail("Phase 2 does not contain exactly the locked 654 source files")
    source_paths = [record["path"] for record in source_records]
    file_paths = [record["path"] for record in file_records]
    if (
        len(source_paths) != len(set(source_paths))
        or len(file_paths) != len(set(file_paths))
        or file_paths != sorted(
        source_paths, key=lambda value: value.encode("utf-8")
        )
    ):
        fail("Source/File manifest path set or order mismatch")
    source_by_path = {record["path"]: record for record in source_records}
    file_by_path = {record["path"]: record for record in file_records}
    chunks_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunk_records:
        chunks_by_path[chunk["path"]].append(chunk)
    tree = git_tree_map(git_executable, repo, target["target"]["commit_sha"])
    raw_by_path: dict[str, bytes] = {}
    frame_by_id: dict[str, bytes] = {}
    chunk_by_id: dict[str, dict[str, Any]] = {}
    verifier_languages = {
        "c": Language(tree_sitter_c.language()),
        "cpp": Language(tree_sitter_cpp.language()),
    }
    verifier_parsers = {
        name: Parser(language) for name, language in verifier_languages.items()
    }
    reader = IndependentBlobReader(git_executable, repo)
    try:
        for path in file_paths:
            source = source_by_path[path]
            file_record = file_by_path[path]
            if tree.get(path) != (source["mode"], source["git_blob_oid"]):
                fail(f"Source manifest path/OID differs from locked Git tree: {path}")
            raw = reader.read(source["git_blob_oid"], source["size_bytes"])
            raw_by_path[path] = raw
            _, encoding = decode(raw)
            if file_record["git_blob_oid"] != source["git_blob_oid"]:
                fail(f"File record OID mismatch: {path}")
            if file_record["raw_size_bytes"] != len(raw):
                fail(f"File record size mismatch: {path}")
            if file_record["raw_content_sha256"] != sha256(raw):
                fail(f"File record content hash mismatch: {path}")
            if file_record["source_encoding"] != encoding:
                fail(f"File record encoding mismatch: {path}")
            expected_units: list[tuple[int, int, str, str, bool]] = []
            if encoding == "utf-8":
                expected_language, root_node, expected_score, expected_attempts = (
                    independently_parse_file(raw, source["extension"], verifier_parsers)
                )
                if file_record["language"] != expected_language:
                    fail(f"Independent grammar selection mismatch: {path}")
                if file_record["selected_parse_score"] != expected_score:
                    fail(f"Independent parse score mismatch: {path}")
                if file_record["parser_attempts"] != expected_attempts:
                    fail(f"Independent parser-attempt evidence mismatch: {path}")
                has_diagnostic = bool(expected_score[1] or expected_score[2] or root_node.has_error)
                expected_outcome = "FALLBACK_SUCCESS" if has_diagnostic else "PARSE_SUCCESS"
                if file_record["parse_outcome"] != expected_outcome:
                    fail(f"Independent parse outcome mismatch: {path}")
                for unit_start, unit_end, unit_node in independent_transparent_units(
                    root_node, 0, len(raw)
                ):
                    unit_kind = independent_kind(unit_node.type)
                    unit_diagnostic = any(
                        item.is_error or item.is_missing for item in walk_nodes(unit_node)
                    )
                    expected_units.append(
                        (unit_start, unit_end, unit_kind, unit_node.type, unit_diagnostic)
                    )
            else:
                if file_record["parser_attempts"] or file_record["selected_parse_score"] is not None:
                    fail(f"Non-UTF-8 file must not claim Tree-sitter attempts: {path}")
            if file_record["parse_outcome"] == "TERMINAL_ERROR":
                fail(f"Terminal parser error published for {path}")
            if file_record["parse_outcome"] == "PARSE_SUCCESS":
                if (
                    file_record["grammar"] not in {"tree-sitter-c", "tree-sitter-cpp"}
                    or file_record["fallback_reason"] is not None
                    or file_record["selected_parse_score"] is None
                    or file_record["selected_parse_score"][:3] != [0, 0, 0]
                ):
                    fail(f"Parse-success semantics mismatch: {path}")
            elif file_record["parse_outcome"] != "FALLBACK_SUCCESS":
                fail(f"Fallback-success semantics mismatch: {path}")
            elif file_record["fallback_reason"] == "non_utf8_source":
                if file_record["grammar"] != "line-window":
                    fail(f"Non-UTF-8 fallback grammar mismatch: {path}")
            elif file_record["fallback_reason"] == "syntax_error_or_missing_node":
                if file_record["grammar"] not in {
                    "tree-sitter-c+line-window", "tree-sitter-cpp+line-window"
                }:
                    fail(f"Hybrid fallback grammar mismatch: {path}")
            elif not file_record["fallback_reason"]:
                fail(f"Fallback reason missing: {path}")
            attempts = file_record["parser_attempts"]
            for attempt in attempts:
                if not isinstance(attempt, dict) or attempt.get("status") not in {"OK", "ERROR"}:
                    fail(f"Malformed parser attempt record: {path}")
                expected_attempt_keys = (
                    {"grammar", "status", "score"}
                    if attempt["status"] == "OK"
                    else {"grammar", "status", "error_type"}
                )
                if set(attempt) != expected_attempt_keys:
                    fail(f"Unexpected parser attempt fields: {path}")
            chunks = sorted(chunks_by_path[path], key=lambda item: item["start_byte"])
            if len(chunks) != file_record["chunk_count"]:
                fail(f"File Chunk count mismatch: {path}")
            cursor = 0
            for ordinal, chunk in enumerate(chunks):
                if chunk["file_chunk_ordinal"] != ordinal:
                    fail(f"File Chunk ordinal mismatch: {path}")
                if chunk["start_byte"] != cursor:
                    fail(f"Coverage gap/overlap before Chunk {chunk['chunk_id']}")
                end = chunk["end_byte_exclusive"]
                if end <= cursor or end > len(raw):
                    fail(f"Invalid Chunk range: {chunk['chunk_id']}")
                content = raw[cursor:end]
                if chunk["raw_size_bytes"] != len(content):
                    fail(f"Chunk raw size mismatch: {chunk['chunk_id']}")
                raw_hash = sha256(content)
                if chunk["raw_content_sha256"] != raw_hash:
                    fail(f"Chunk raw hash mismatch: {chunk['chunk_id']}")
                identity_value = {
                    "target_commit_sha": chunk["target_commit_sha"],
                    "target_scope_sha256": chunk["target_scope_sha256"],
                    "path": path,
                    "git_blob_oid": chunk["git_blob_oid"],
                    "kind": chunk["kind"],
                    "start_byte": cursor,
                    "end_byte_exclusive": end,
                    "raw_content_sha256": raw_hash,
                }
                identity = sha256(CHUNK_DOMAIN + canonical_json(identity_value, newline=False))
                if chunk["chunk_identity_sha256"] != identity or chunk["chunk_id"] != f"C1-{identity[:24]}":
                    fail(f"Chunk identity mismatch: {chunk['chunk_id']}")
                if chunk["target_commit_sha"] != target["target"]["commit_sha"]:
                    fail(f"Chunk target commit mismatch: {chunk['chunk_id']}")
                if chunk["target_scope_sha256"] != target["analysis_scope"]["scope_sha256"]:
                    fail(f"Chunk target scope mismatch: {chunk['chunk_id']}")
                if chunk["git_blob_oid"] != source["git_blob_oid"]:
                    fail(f"Chunk blob OID mismatch: {chunk['chunk_id']}")
                if chunk["source_encoding"] != file_record["source_encoding"]:
                    fail(f"Chunk source encoding mismatch: {chunk['chunk_id']}")
                if chunk["language"] != file_record["language"]:
                    fail(f"Chunk language mismatch: {chunk['chunk_id']}")
                if chunk["parse_mode"] != file_record["parse_outcome"]:
                    fail(f"Chunk parse mode mismatch: {chunk['chunk_id']}")
                if (chunk["start_line"], chunk["start_column_byte"]) != line_column(raw, cursor):
                    fail(f"Chunk start line/column mismatch: {chunk['chunk_id']}")
                if chunk["end_line"] != line_column(raw, end - 1)[0]:
                    fail(f"Chunk inclusive end line mismatch: {chunk['chunk_id']}")
                if (
                    chunk["end_point_line"], chunk["end_column_byte_exclusive"]
                ) != line_column(raw, end):
                    fail(f"Chunk end line/column mismatch: {chunk['chunk_id']}")
                unit_start = chunk["structural_unit_start_byte"]
                unit_end = chunk["structural_unit_end_byte_exclusive"]
                if not (0 <= unit_start <= cursor < end <= unit_end <= len(raw)):
                    fail(f"Chunk structural-unit range mismatch: {chunk['chunk_id']}")
                validate_chunk_semantics(chunk)
                rendered = content.decode(encoding, errors="strict")
                rendered_raw = rendered.encode("utf-8")
                if chunk["rendered_content_sha256"] != sha256(rendered_raw):
                    fail(f"Rendered content hash mismatch: {chunk['chunk_id']}")
                if chunk["rendered_content_utf8_bytes"] != len(rendered_raw):
                    fail(f"Rendered content size mismatch: {chunk['chunk_id']}")
                frame = render_frame(chunk, rendered)
                if chunk["evidence_frame_sha256"] != sha256(frame):
                    fail(f"Evidence frame hash mismatch: {chunk['chunk_id']}")
                if chunk["evidence_frame_utf8_bytes"] != len(frame):
                    fail(f"Evidence frame size mismatch: {chunk['chunk_id']}")
                if chunk["token_count"] != len(frame) or len(frame) > MAX_PAYLOAD:
                    fail(f"Chunk budget mismatch: {chunk['chunk_id']}")
                if chunk["chunk_id"] in chunk_by_id:
                    fail(f"Duplicate Chunk ID: {chunk['chunk_id']}")
                chunk_by_id[chunk["chunk_id"]] = chunk
                frame_by_id[chunk["chunk_id"]] = frame
                cursor = end
            if cursor != len(raw) or file_record["covered_bytes"] != len(raw):
                fail(f"Source blob is not exactly reconstructed by Chunks: {path}")
            if file_record["unmapped_ranges"] != 0 or file_record["overlap_ranges"] != 0:
                fail(f"File record reports incomplete coverage: {path}")
            for unit_start, unit_end, unit_kind, unit_node_type, unit_diagnostic in expected_units:
                if unit_kind not in PROTECTED_KINDS:
                    continue
                references = [
                    chunk
                    for chunk in chunks
                    if chunk["structural_unit_start_byte"] == unit_start
                    and chunk["structural_unit_end_byte_exclusive"] == unit_end
                    and chunk["structural_unit_kind"] == unit_kind
                ]
                if not references:
                    fail(f"Protected structural unit has no dedicated Chunk ancestry: {path}:{unit_start}")
                if unit_end - unit_start <= contract["chunking"]["max_rendered_content_utf8_bytes"]:
                    if len(references) != 1:
                        fail(f"Small protected structural unit was split: {path}:{unit_start}")
                    reference = references[0]
                    if unit_diagnostic:
                        if reference["kind"] != "LINE_WINDOW":
                            fail(f"Diagnostic structural unit did not use local fallback: {path}:{unit_start}")
                    elif (
                        reference["kind"] != unit_kind
                        or reference["node_type"] != unit_node_type
                        or reference["start_byte"] != unit_start
                        or reference["end_byte_exclusive"] != unit_end
                    ):
                        fail(f"Protected structural unit boundary/kind drift: {path}:{unit_start}")
    finally:
        reader.close()

    expected_chunk_order = sorted(
        chunk_records,
        key=lambda item: (
            item["path"].encode("utf-8"), item["start_byte"],
            item["end_byte_exclusive"], item["chunk_id"],
        ),
    )
    if chunk_records != expected_chunk_order:
        fail("Chunk manifest order is not canonical source order")
    expected_groups: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for chunk in chunk_records:
        size = len(frame_by_id[chunk["chunk_id"]])
        if current and current_size + size > MAX_PAYLOAD:
            expected_groups.append(current)
            current = []
            current_size = 0
        current.append(chunk["chunk_id"])
        current_size += size
    if current:
        expected_groups.append(current)
    if len(batch_records) != len(expected_groups):
        fail("Batch count differs from deterministic next-fit packing")
    flattened: list[str] = []
    contract_hash = sha256((chunking_dir / "chunking_contract.json").read_bytes())
    expected_transitive = {
        "source_manifest_sha256": sha256(source_raw),
        "chunking_contract_sha256": contract_hash,
        "file_manifest_sha256": sha256(file_raw),
        "chunk_manifest_sha256": sha256(chunk_raw),
    }
    seen_batches: set[str] = set()
    for ordinal, (batch, expected_ids) in enumerate(zip(batch_records, expected_groups)):
        if batch["batch_ordinal"] != ordinal or batch["chunk_ids"] != expected_ids:
            fail(f"Batch membership/order drift at ordinal {ordinal}")
        if batch["chunk_count"] != len(expected_ids):
            fail(f"Batch Chunk count mismatch: {batch['batch_id']}")
        for key, expected in expected_transitive.items():
            if batch[key] != expected:
                fail(f"Batch transitive binding mismatch: {key}")
        expected_batch_constants = {
            "target_commit_sha": target["target"]["commit_sha"],
            "target_scope_sha256": target["analysis_scope"]["scope_sha256"],
            "renderer_version": "evidence-frame-v1",
            "budget_counter": "utf8_byte_upper_bound_v1",
            "budget_unit_rule": "one_budget_unit_per_utf8_byte",
            "max_payload_utf8_bytes": 8192,
            "token_count_kind": "conservative_upper_bound",
        }
        for key, expected in expected_batch_constants.items():
            if batch[key] != expected:
                fail(f"Batch constant mismatch: {key}")
        payload = b"".join(frame_by_id[chunk_id] for chunk_id in expected_ids)
        identities = b"\0".join(
            chunk_by_id[chunk_id]["chunk_identity_sha256"].encode("ascii")
            for chunk_id in expected_ids
        )
        ordered_hash = sha256(identities)
        payload_hash = sha256(payload)
        identity_value = {
            "target_commit_sha": target["target"]["commit_sha"],
            "target_scope_sha256": target["analysis_scope"]["scope_sha256"],
            "budget_counter": "utf8_byte_upper_bound_v1",
            "max_payload_utf8_bytes": 8192,
            "ordered_chunk_identity_sha256": ordered_hash,
            "payload_sha256": payload_hash,
            "payload_utf8_bytes": len(payload),
        }
        identity = sha256(BATCH_DOMAIN + canonical_json(identity_value, newline=False))
        if batch["batch_identity_sha256"] != identity or batch["batch_id"] != f"B1-{identity[:24]}":
            fail(f"Batch identity mismatch: {batch['batch_id']}")
        if batch["ordered_chunk_identity_sha256"] != ordered_hash:
            fail(f"Batch ordered Chunk hash mismatch: {batch['batch_id']}")
        if batch["payload_sha256"] != payload_hash:
            fail(f"Batch payload hash mismatch: {batch['batch_id']}")
        if batch["payload_utf8_bytes"] != len(payload) or batch["token_count"] != len(payload):
            fail(f"Batch payload size mismatch: {batch['batch_id']}")
        if len(payload) > MAX_PAYLOAD:
            fail(f"Batch exceeds locked evidence cap: {batch['batch_id']}")
        if batch["batch_id"] in seen_batches:
            fail(f"Duplicate Batch ID: {batch['batch_id']}")
        seen_batches.add(batch["batch_id"])
        flattened.extend(expected_ids)
    if flattened != [chunk["chunk_id"] for chunk in chunk_records]:
        fail("Chunks are missing or duplicated across Batches")

    evaluation = experiment["semantic"]["evaluation_contract"]
    expected_selection_keys = {
        "schema_version", "target_commit_sha", "target_scope_sha256",
        "batch_manifest_path", "batch_manifest_sha256", "selection_seed",
        "selection_seed_encoding", "selection_algorithm", "selection_hash_domain",
        "batch_count", "selected_batch_ids", "frozen_before_any_llm_result",
        "replace_after_result_observation",
    }
    if set(selection) != expected_selection_keys:
        fail("Selection artifact fields drifted")
    if selection["batch_manifest_sha256"] != sha256(batch_raw):
        fail("Selection Batch manifest hash mismatch")
    domain = evaluation["selection_hash_domain"].encode("utf-8")
    seed = str(evaluation["selection_seed"]).encode("ascii")
    ranked = sorted(
        [batch["batch_id"] for batch in batch_records],
        key=lambda batch_id: (
            sha256(domain + b"\0" + seed + b"\0" + batch_id.encode("utf-8")),
            batch_id,
        ),
    )
    if selection["selected_batch_ids"] != ranked[:3]:
        fail("Selected Batches differ from locked result-independent ranking")
    if len(set(selection["selected_batch_ids"])) != 3:
        fail("Selection does not contain three unique Batches")
    expected_selection_values = {
        "schema_version": 1,
        "target_commit_sha": target["target"]["commit_sha"],
        "target_scope_sha256": target["analysis_scope"]["scope_sha256"],
        "batch_manifest_path": evaluation["batch_manifest_artifact"],
        "selection_seed": evaluation["selection_seed"],
        "selection_seed_encoding": evaluation["selection_seed_encoding"],
        "selection_algorithm": evaluation["selection_algorithm"],
        "selection_hash_domain": evaluation["selection_hash_domain"],
        "batch_count": 3,
        "frozen_before_any_llm_result": True,
        "replace_after_result_observation": False,
    }
    for key, expected in expected_selection_values.items():
        if selection[key] != expected:
            fail(f"Selection contract mismatch: {key}")

    outcomes = Counter(record["parse_outcome"] for record in file_records)
    kinds = Counter(record["kind"] for record in chunk_records)
    expected_coverage = {
        "schema_version": 1,
        "target_commit_sha": target["target"]["commit_sha"],
        "target_scope_sha256": target["analysis_scope"]["scope_sha256"],
        "source_file_count": expected_count,
        "parse_success": outcomes["PARSE_SUCCESS"],
        "fallback_success": outcomes["FALLBACK_SUCCESS"],
        "parse_error": outcomes["TERMINAL_ERROR"],
        "terminal_error": outcomes["TERMINAL_ERROR"],
        "status_sum": sum(outcomes.values()),
        "chunk_count": len(chunk_records),
        "batch_count": len(batch_records),
        "mapped_source_bytes": sum(record["covered_bytes"] for record in file_records),
        "source_bytes": sum(record["raw_size_bytes"] for record in file_records),
        "unmapped_in_scope_ranges": 0,
        "overlap_in_scope_ranges": 0,
        "duplicate_chunk_ids": 0,
        "oversized_chunks": 0,
        "oversized_batches": 0,
        "chunk_kind_counts": dict(sorted(kinds.items())),
    }
    if coverage != expected_coverage:
        fail("Coverage report does not match independently reconstructed counts")
    if coverage["status_sum"] != 654 or coverage["terminal_error"] != 0:
        fail("Phase 2 terminal file status gate failed")
    if coverage["mapped_source_bytes"] != coverage["source_bytes"]:
        fail("Phase 2 byte coverage gate failed")
    if set(report) != {
        "schema_version", "status", "target_commit_sha", "target_scope_sha256",
        "counts", "artifact_sha256", "reproducibility",
    } or report["schema_version"] != 1:
        fail("Phase 2 report shape drift")
    if (
        report["status"] != "PASS"
        or report["counts"] != coverage
        or report["target_commit_sha"] != target["target"]["commit_sha"]
        or report["target_scope_sha256"] != target["analysis_scope"]["scope_sha256"]
    ):
        fail("Phase 2 report status/counts mismatch")
    expected_hashes = {
        "chunking_contract.json": contract_hash,
        "file_manifest.jsonl": sha256(file_raw),
        "chunk_manifest.jsonl": sha256(chunk_raw),
        "batch_manifest.jsonl": sha256(batch_raw),
        "coverage_report.json": sha256((chunking_dir / "coverage_report.json").read_bytes()),
        "selection.json": sha256(selection_path.read_bytes()),
    }
    if report["artifact_sha256"] != expected_hashes:
        fail("Phase 2 report artifact hash mismatch")
    if report["reproducibility"] != {
        "method": "two_independent_in_memory_builds_from_locked_git_blobs",
        "byte_identical": True,
        "compared_artifacts": [
            "chunking_contract.json", "file_manifest.jsonl", "chunk_manifest.jsonl",
            "batch_manifest.jsonl", "coverage_report.json", "selection.json",
        ],
    }:
        fail("Phase 2 reproducibility claim drifted")

    if check_lock:
        semantic = experiment["semantic"]
        if semantic["lock_stage"] != "evaluation_frozen":
            fail("Experiment is not in evaluation_frozen stage")
        binding = semantic.get("evaluation_binding")
        expected_binding = {
            "batch_manifest_path": "artifacts/chunking/batch_manifest.jsonl",
            "batch_manifest_sha256": sha256(batch_raw),
            "selection_path": "artifacts/evaluation/selection.json",
            "selection_sha256": sha256(selection_path.read_bytes()),
        }
        if binding != expected_binding:
            fail("Experiment evaluation binding mismatch")
        semantic_hash = sha256(EXPERIMENT_DOMAIN + canonical_json(semantic, newline=False))
        if experiment["identity"]["semantic_sha256"] != semantic_hash:
            fail("Frozen Experiment semantic hash mismatch")
        if experiment["identity"]["experiment_id"] != "exp-v1-" + semantic_hash[:24]:
            fail("Frozen Experiment ID mismatch")
        completed = subprocess.run(
            [sys.executable, str(root / "scripts" / "verify_experiment_lock.py")],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            fail("Phase 1/2 Experiment verifier failed: " + (completed.stdout + completed.stderr).strip())

    if rebuild_check:
        scratch_root = root / "tmp"
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phase2-rebuild-", dir=scratch_root) as temp:
            output = Path(temp) / "chunking"
            command = [
                sys.executable,
                str(root / "scripts" / "build_phase2.py"),
                "--project-root", str(root),
                "--repo", str(repo),
                "--git", str(git_executable),
                "--output-dir", str(output),
            ]
            completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
            if completed.returncode != 0:
                fail("Deterministic rebuild failed: " + (completed.stdout + completed.stderr).strip())
            for name in PHASE2_NAMES:
                if (output / name).read_bytes() != (chunking_dir / name).read_bytes():
                    fail(f"Canonical artifact differs from isolated rebuild: {name}")
            if (output / "selection.json").read_bytes() != selection_path.read_bytes():
                fail("Canonical selection differs from isolated rebuild")
    return {
        "status": "PASS",
        "files": len(file_records),
        "chunks": len(chunk_records),
        "batches": len(batch_records),
        "parse_success": outcomes["PARSE_SUCCESS"],
        "fallback_success": outcomes["FALLBACK_SUCCESS"],
        "selected_batch_ids": selection["selected_batch_ids"],
        "experiment_id": experiment["identity"]["experiment_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--git", type=Path)
    parser.add_argument("--skip-rebuild-check", action="store_true")
    arguments = parser.parse_args()
    try:
        root = arguments.project_root.resolve()
        repo = (
            arguments.repo.resolve()
            if arguments.repo is not None
            else (root / ".target-src" / "userland").resolve()
        )
        result = verify(
            root=root,
            repo=repo,
            git_executable=find_git(arguments.git),
            rebuild_check=not arguments.skip_rebuild_check,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (VerificationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Phase 2 verification FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
