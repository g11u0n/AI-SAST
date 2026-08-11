"""Conservative name-candidate resolution for syntactic index relations."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Iterable


def _symbol_key(record: dict[str, Any]) -> tuple[bytes, int, str]:
    return (
        record["path"].encode("utf-8"),
        record["reference"]["extent"]["start_byte"],
        record["symbol_identity_sha256"],
    )


def _relation_key(record: dict[str, Any]) -> tuple[bytes, int, str]:
    return (
        record["path"].encode("utf-8"),
        record["reference"]["extent"]["start_byte"],
        record.get("call_identity_sha256", record.get("include_identity_sha256", "")),
    )


def resolve_call_edges(
    calls: Iterable[dict[str, Any]],
    symbols: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve only deterministic name candidates; never claim compiler binding."""

    symbol_list = list(symbols)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_qualified: dict[str, list[dict[str, Any]]] = defaultdict(list)
    symbol_ids: set[str] = set()
    for symbol in symbol_list:
        if symbol["symbol_id"] in symbol_ids:
            raise ValueError(f"Duplicate Symbol ID: {symbol['symbol_id']}")
        symbol_ids.add(symbol["symbol_id"])
        by_name[symbol["name"]].append(symbol)
        by_qualified[symbol["qualified_name"]].append(symbol)
    for mapping in (by_name, by_qualified):
        for name in mapping:
            mapping[name].sort(key=_symbol_key)

    output: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    for original in calls:
        record = dict(original)
        if record["call_id"] in call_ids:
            raise ValueError(f"Duplicate call occurrence: {record['call_id']}")
        call_ids.add(record["call_id"])
        if record["caller_symbol_id"] not in symbol_ids:
            raise ValueError(f"Dangling call caller: {record['call_id']}")
        name = record["callee_name"]
        named = [] if name is None else list(by_name.get(name, ()))
        qualified = list(by_qualified.get(record["callee_expression"], ()))
        pool = qualified if qualified else named
        definitions = [
            item
            for item in pool
            if item["symbol_kind"] == "FUNCTION" and item["role"] == "DEFINITION"
        ]
        declarations = [
            item
            for item in pool
            if item["symbol_kind"] == "FUNCTION" and item["role"] == "DECLARATION"
        ]
        macros = [
            item
            for item in named
            if item["symbol_kind"] == "MACRO" and item["role"] == "DEFINITION"
        ]
        definitions.sort(key=_symbol_key)
        declarations.sort(key=_symbol_key)
        macros.sort(key=_symbol_key)
        record["candidate_definition_ids"] = [
            item["symbol_id"] for item in definitions
        ]
        record["declaration_candidate_ids"] = [
            item["symbol_id"] for item in declarations
        ]
        if record["syntactic_kind"] == "INDIRECT" or name is None:
            resolution = "INDIRECT_OR_DYNAMIC"
        elif macros and (definitions or declarations):
            resolution = "MACRO_OR_FUNCTION_AMBIGUOUS"
        elif record["syntactic_kind"] in {"MEMBER", "QUALIFIED"}:
            resolution = "MEMBER_OR_QUALIFIED"
        else:
            same_file_internal = [
                item
                for item in definitions
                if item["path"] == record["path"] and item["linkage"] == "INTERNAL"
            ]
            if len(same_file_internal) == 1:
                record["candidate_definition_ids"] = [
                    same_file_internal[0]["symbol_id"]
                ]
                resolution = "UNIQUE_FILE_LOCAL_CANDIDATE"
            elif len(same_file_internal) > 1:
                resolution = "AMBIGUOUS"
            elif len(definitions) == 1:
                resolution = "UNIQUE_REPOSITORY_CANDIDATE"
            elif len(definitions) > 1:
                resolution = "AMBIGUOUS"
            else:
                resolution = "UNRESOLVED_EXTERNAL"
        record["resolution"] = resolution
        output.append(record)
    output.sort(key=_relation_key)
    return output


def resolve_include_edges(
    includes: Iterable[dict[str, Any]],
    source_paths: Iterable[str],
) -> list[dict[str, Any]]:
    """Resolve include spellings to path candidates without build-config claims."""

    paths = sorted(set(source_paths), key=lambda value: value.encode("utf-8"))
    path_set = set(paths)
    by_basename: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        by_basename[PurePosixPath(path).name].append(path)
    output: list[dict[str, Any]] = []
    include_ids: set[str] = set()
    for original in includes:
        record = dict(original)
        if record["include_id"] in include_ids:
            raise ValueError(f"Duplicate include occurrence: {record['include_id']}")
        include_ids.add(record["include_id"])
        target = record["normalized_target"]
        candidates: list[str] = []
        if target and not target.startswith("/") and ".." not in PurePosixPath(target).parts:
            local = str(PurePosixPath(record["path"]).parent / target)
            if local in path_set:
                candidates.append(local)
            if target in path_set and target not in candidates:
                candidates.append(target)
            for path in by_basename.get(PurePosixPath(target).name, ()):
                if path not in candidates:
                    candidates.append(path)
        candidates.sort(key=lambda value: value.encode("utf-8"))
        record["resolved_paths"] = candidates
        if not candidates:
            record["resolution"] = "UNRESOLVED_WITHOUT_BUILD_CONFIG"
        elif len(candidates) == 1:
            record["resolution"] = "UNIQUE_PATH_CANDIDATE"
        else:
            record["resolution"] = "AMBIGUOUS_PATH_CANDIDATES"
        output.append(record)
    output.sort(key=_relation_key)
    return output
