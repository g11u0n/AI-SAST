"""Mechanically extract symbols, syntactic calls, includes, and AST facts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

from .code_index import ChunkTable, canonical_json, normalized_text, sha256


SYMBOL_ID_DOMAIN = b"ai-sast-symbol-id-v1\0"
CALL_ID_DOMAIN = b"ai-sast-call-id-v1\0"
INCLUDE_ID_DOMAIN = b"ai-sast-include-id-v1\0"
SIGNATURE_MAX_UTF8_BYTES = 512
PARAMETER_MAX_UTF8_BYTES = 192

TYPE_NODE_KINDS = {
    "type_definition": "TYPEDEF",
    "struct_specifier": "STRUCT",
    "union_specifier": "UNION",
    "enum_specifier": "ENUM",
    "class_specifier": "CLASS",
}
NAME_NODE_TYPES = {
    "identifier",
    "field_identifier",
    "type_identifier",
    "namespace_identifier",
    "operator_name",
    "destructor_name",
}
DECLARATOR_NODE_TYPES = {
    "identifier",
    "field_identifier",
    "qualified_identifier",
    "scoped_identifier",
    "function_declarator",
    "pointer_declarator",
    "reference_declarator",
    "array_declarator",
    "parenthesized_declarator",
    "init_declarator",
    "attributed_declarator",
}
CONDITIONAL_WRAPPERS = {
    "preproc_if",
    "preproc_ifdef",
    "preproc_ifndef",
    "preproc_elif",
    "preproc_else",
}
CONDITION_NODES = {
    "if_statement",
    "switch_statement",
    "conditional_expression",
}
LOOP_NODES = {
    "for_statement",
    "for_range_loop",
    "while_statement",
    "do_statement",
}
FACT_NODE_MAP = {
    "call_expression": "calls",
    "pointer_expression": "pointer_operations",
    "subscript_expression": "array_accesses",
    "if_statement": "conditions",
    "switch_statement": "conditions",
    "conditional_expression": "conditions",
    "for_statement": "loops",
    "for_range_loop": "loops",
    "while_statement": "loops",
    "do_statement": "loops",
    "return_statement": "returns",
    "assignment_expression": "assignments",
    "update_expression": "updates",
    "field_expression": "field_accesses",
    "cast_expression": "casts",
    "sizeof_expression": "sizeof_expressions",
    "goto_statement": "gotos",
}
FACT_KEYS = tuple(sorted(set(FACT_NODE_MAP.values())))


@dataclass(frozen=True)
class FileExtraction:
    file_record: dict[str, Any]
    symbols: tuple[dict[str, Any], ...]
    call_occurrences: tuple[dict[str, Any], ...]
    include_edges: tuple[dict[str, Any], ...]
    chunk_facts: tuple[dict[str, Any], ...]


def _walk(root: Node) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def _clean(node: Node) -> bool:
    return not (node.is_error or node.is_missing or node.has_error)


def _first_descendant(node: Node, node_types: set[str]) -> Node | None:
    for candidate in _walk(node):
        if candidate.type in node_types:
            return candidate
    return None


def _last_name_descendant(node: Node) -> Node | None:
    matches = [
        candidate
        for candidate in _walk(node)
        if candidate.type in NAME_NODE_TYPES and _clean(candidate)
    ]
    return matches[-1] if matches else None


def _declarator_name_node(node: Node) -> Node | None:
    """Follow declarator fields before considering parameter-name descendants."""

    current = node
    visited: set[tuple[int, int, str]] = set()
    while True:
        key = (current.start_byte, current.end_byte, current.type)
        if key in visited:
            return None
        visited.add(key)
        if current.type in NAME_NODE_TYPES:
            return current
        nested = _field(current, "declarator")
        if nested is not None and nested.end_byte > nested.start_byte:
            current = nested
            continue
        name = _field(current, "name")
        if name is not None and name.end_byte > name.start_byte:
            current = name
            continue
        candidates = [
            child
            for child in current.named_children
            if child.type in DECLARATOR_NODE_TYPES
            and child.end_byte > child.start_byte
        ]
        if candidates:
            current = candidates[0]
            continue
        return _last_name_descendant(current)


def _field(node: Node, name: str) -> Node | None:
    try:
        return node.child_by_field_name(name)
    except (AttributeError, ValueError):
        return None


def _node_text(raw: bytes, node: Node, encoding: str) -> str:
    return raw[node.start_byte : node.end_byte].decode(encoding, "strict")


def _scope_name(raw: bytes, node: Node, encoding: str) -> tuple[str, Node] | None:
    name = _field(node, "name")
    if name is None:
        name = _last_name_descendant(node)
    if name is None or not _clean(name):
        return None
    return _node_text(raw, name, encoding), name


def _declarator_name(raw: bytes, declarator: Node, encoding: str) -> tuple[str, str, Node] | None:
    name_node = _declarator_name_node(declarator)
    if name_node is None:
        return None
    base_name = _node_text(raw, name_node, encoding)
    declared = _node_text(raw, declarator, encoding)
    declared = "".join(declared.split())
    qualified = declared
    for prefix in ("*", "&", "&&"):
        qualified = qualified.lstrip(prefix)
    while qualified.startswith("(") and qualified.endswith(")") and len(qualified) > 2:
        qualified = qualified[1:-1]
    if "(" in qualified:
        qualified = qualified.split("(", 1)[0]
    if "[" in qualified:
        qualified = qualified.split("[", 1)[0]
    if not qualified or len(qualified.encode("utf-8")) > 256:
        qualified = base_name
    return base_name, qualified, name_node


def _signature_range(node: Node) -> tuple[int, int]:
    body = _field(node, "body")
    if body is not None and body.start_byte > node.start_byte:
        return node.start_byte, body.start_byte
    return node.start_byte, node.end_byte


def _parameter_records(raw: bytes, node: Node, encoding: str) -> list[str]:
    parameter_list = _first_descendant(node, {"parameter_list"})
    if parameter_list is None:
        return []
    output: list[str] = []
    for child in parameter_list.named_children:
        if child.type not in {
            "parameter_declaration",
            "optional_parameter_declaration",
            "variadic_parameter",
        }:
            continue
        display, _, _ = normalized_text(
            raw[child.start_byte : child.end_byte],
            encoding,
            max_utf8_bytes=PARAMETER_MAX_UTF8_BYTES,
        )
        output.append(display)
    return output


def _subtree_facts(node: Node) -> dict[str, int]:
    facts = {key: 0 for key in FACT_KEYS}
    for candidate in _walk(node):
        if not _clean(candidate):
            continue
        fact = FACT_NODE_MAP.get(candidate.type)
        if fact is not None:
            facts[fact] += 1
    return facts


class SymbolExtractor:
    """Extract only syntax-backed navigation facts from a single Git blob."""

    def __init__(self) -> None:
        self.languages = {
            "c": Language(tree_sitter_c.language()),
            "cpp": Language(tree_sitter_cpp.language()),
        }
        self.parsers = {
            name: Parser(language) for name, language in self.languages.items()
        }

    def extract(
        self,
        *,
        raw: bytes,
        source_record: dict[str, Any],
        phase2_file: dict[str, Any],
        chunks: tuple[dict[str, Any], ...],
        target_commit_sha: str,
        target_scope_sha256: str,
        index_contract_sha256: str,
    ) -> FileExtraction:
        table = ChunkTable(chunks)
        if table.size_bytes != len(raw):
            raise ValueError("Phase 2 Chunk partition does not match the Git blob")
        encoding = phase2_file["source_encoding"]
        common = {
            "target_commit_sha": target_commit_sha,
            "target_scope_sha256": target_scope_sha256,
            "index_contract_sha256": index_contract_sha256,
            "path": source_record["path"],
            "git_blob_oid": source_record["git_blob_oid"],
        }
        facts_by_chunk = {
            chunk["chunk_id"]: {
                "schema_version": 1,
                **common,
                "chunk_id": chunk["chunk_id"],
                "chunk_content_sha256": chunk["raw_content_sha256"],
                "ast_available": encoding == "utf-8",
                "extraction_mode": (
                    "NO_AST_NON_UTF8"
                    if encoding != "utf-8"
                    else (
                        "CLEAN_AST"
                        if phase2_file["parse_outcome"] == "PARSE_SUCCESS"
                        else "PARTIAL_DIAGNOSTIC_AST"
                    )
                ),
                "node_counts": {key: 0 for key in FACT_KEYS},
                "defined_symbol_ids": [],
                "outgoing_call_ids": [],
            }
            for chunk in chunks
        }
        if encoding != "utf-8":
            return FileExtraction(
                file_record=self._file_record(
                    common=common,
                    phase2_file=phase2_file,
                    status="NO_AST_NON_UTF8",
                    diagnostics={"error_nodes": 0, "missing_nodes": 0},
                    symbol_count=0,
                    call_count=0,
                    include_count=0,
                    chunk_count=len(chunks),
                ),
                symbols=(),
                call_occurrences=(),
                include_edges=(),
                chunk_facts=tuple(facts_by_chunk.values()),
            )

        language = phase2_file["language"]
        if language not in self.parsers:
            raise ValueError(f"Unsupported Phase 2 grammar: {language}")
        tree = self.parsers[language].parse(raw, encoding="utf8")
        diagnostics = {
            "error_nodes": sum(1 for node in _walk(tree.root_node) if node.is_error),
            "missing_nodes": sum(1 for node in _walk(tree.root_node) if node.is_missing),
        }
        for node in _walk(tree.root_node):
            if not _clean(node):
                continue
            fact_name = FACT_NODE_MAP.get(node.type)
            if fact_name is None or node.start_byte >= len(raw):
                continue
            facts_by_chunk[table.containing(node.start_byte)["chunk_id"]]["node_counts"][
                fact_name
            ] += 1

        symbols: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        includes: list[dict[str, Any]] = []
        self._visit(
            node=tree.root_node,
            raw=raw,
            encoding=encoding,
            table=table,
            common=common,
            scopes=(),
            current_function_id=None,
            conditional_depth=0,
            symbols=symbols,
            calls=calls,
            includes=includes,
        )
        symbol_ids = {record["symbol_id"] for record in symbols}
        if len(symbol_ids) != len(symbols):
            raise ValueError("Duplicate Symbol identity within one file")
        for symbol in symbols:
            facts_by_chunk[symbol["reference"]["anchor"]["chunk_id"]][
                "defined_symbol_ids"
            ].append(symbol["symbol_id"])
        for call in calls:
            facts_by_chunk[call["reference"]["anchor"]["chunk_id"]][
                "outgoing_call_ids"
            ].append(call["call_id"])
        for record in facts_by_chunk.values():
            record["defined_symbol_ids"].sort()
            record["outgoing_call_ids"].sort()

        status = (
            "INDEXED"
            if diagnostics["error_nodes"] == 0 and diagnostics["missing_nodes"] == 0
            else "PARTIAL_DIAGNOSTIC"
        )
        return FileExtraction(
            file_record=self._file_record(
                common=common,
                phase2_file=phase2_file,
                status=status,
                diagnostics=diagnostics,
                symbol_count=len(symbols),
                call_count=len(calls),
                include_count=len(includes),
                chunk_count=len(chunks),
            ),
            symbols=tuple(symbols),
            call_occurrences=tuple(calls),
            include_edges=tuple(includes),
            chunk_facts=tuple(facts_by_chunk.values()),
        )

    @staticmethod
    def _file_record(
        *,
        common: dict[str, Any],
        phase2_file: dict[str, Any],
        status: str,
        diagnostics: dict[str, int],
        symbol_count: int,
        call_count: int,
        include_count: int,
        chunk_count: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            **common,
            "language": phase2_file["language"],
            "source_encoding": phase2_file["source_encoding"],
            "phase2_parse_outcome": phase2_file["parse_outcome"],
            "index_status": status,
            "diagnostics": diagnostics,
            "raw_content_sha256": phase2_file["raw_content_sha256"],
            "raw_size_bytes": phase2_file["raw_size_bytes"],
            "chunk_count": chunk_count,
            "symbol_count": symbol_count,
            "call_count": call_count,
            "include_count": include_count,
        }

    def _visit(
        self,
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        scopes: tuple[str, ...],
        current_function_id: str | None,
        conditional_depth: int,
        symbols: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        includes: list[dict[str, Any]],
    ) -> None:
        child_scopes = scopes
        child_function_id = current_function_id
        child_conditional_depth = conditional_depth + (
            1 if node.type in CONDITIONAL_WRAPPERS else 0
        )

        if node.type == "function_definition":
            child_function_id = None
            if _clean(node):
                record = self._function_symbol(
                    node=node,
                    raw=raw,
                    encoding=encoding,
                    table=table,
                    common=common,
                    scopes=scopes,
                    role="DEFINITION",
                    conditional_depth=conditional_depth,
                )
                if record is not None:
                    symbols.append(record)
                    child_function_id = record["symbol_id"]
        elif node.type == "declaration" and current_function_id is None and _clean(node):
            symbols.extend(
                self._declaration_symbols(
                    node=node,
                    raw=raw,
                    encoding=encoding,
                    table=table,
                    common=common,
                    scopes=scopes,
                    conditional_depth=conditional_depth,
                )
            )
        elif node.type in TYPE_NODE_KINDS and _clean(node):
            type_record = self._type_symbol(
                node=node,
                raw=raw,
                encoding=encoding,
                table=table,
                common=common,
                scopes=scopes,
                conditional_depth=conditional_depth,
            )
            if type_record is not None:
                symbols.append(type_record)
                if node.type == "class_specifier":
                    child_scopes = scopes + (type_record["name"],)
        elif node.type in {"preproc_def", "preproc_function_def"} and _clean(node):
            macro = self._macro_symbol(
                node=node,
                raw=raw,
                encoding=encoding,
                table=table,
                common=common,
                scopes=scopes,
                conditional_depth=conditional_depth,
            )
            if macro is not None:
                symbols.append(macro)
        elif node.type == "namespace_definition" and _clean(node):
            namespace = self._namespace_symbol(
                node=node,
                raw=raw,
                encoding=encoding,
                table=table,
                common=common,
                scopes=scopes,
                conditional_depth=conditional_depth,
            )
            if namespace is not None:
                symbols.append(namespace)
                child_scopes = scopes + (namespace["name"],)
        elif node.type == "preproc_include" and _clean(node):
            include = self._include_edge(
                node=node,
                raw=raw,
                encoding=encoding,
                table=table,
                common=common,
                conditional_depth=conditional_depth,
            )
            if include is not None:
                includes.append(include)

        if (
            node.type == "call_expression"
            and current_function_id is not None
            and _clean(node)
        ):
            call = self._call_occurrence(
                node=node,
                caller_symbol_id=current_function_id,
                raw=raw,
                encoding=encoding,
                table=table,
                common=common,
                conditional_depth=conditional_depth,
            )
            if call is not None:
                calls.append(call)

        for child in node.named_children:
            self._visit(
                node=child,
                raw=raw,
                encoding=encoding,
                table=table,
                common=common,
                scopes=child_scopes,
                current_function_id=child_function_id,
                conditional_depth=child_conditional_depth,
                symbols=symbols,
                calls=calls,
                includes=includes,
            )

    def _function_symbol(
        self,
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        scopes: tuple[str, ...],
        role: str,
        conditional_depth: int,
    ) -> dict[str, Any] | None:
        declarator = _field(node, "declarator")
        if declarator is None:
            declarator = _first_descendant(node, {"function_declarator"})
        if declarator is None:
            return None
        name_data = _declarator_name(raw, declarator, encoding)
        if name_data is None:
            return None
        name, declared_name, name_node = name_data
        qualified = declared_name if "::" in declared_name else "::".join(scopes + (name,))
        signature_start, signature_end = _signature_range(node)
        signature, signature_sha, truncated = normalized_text(
            raw[signature_start:signature_end],
            encoding,
            max_utf8_bytes=SIGNATURE_MAX_UTF8_BYTES,
        )
        storage = [
            _node_text(raw, candidate, encoding)
            for candidate in node.named_children
            if candidate.type == "storage_class_specifier"
        ]
        linkage = "INTERNAL" if "static" in storage else "UNSPECIFIED"
        return self._symbol_record(
            node=node,
            name_node=name_node,
            name=name,
            qualified_name=qualified,
            symbol_kind="FUNCTION",
            role=role,
            signature=signature,
            signature_sha256=signature_sha,
            signature_truncated=truncated,
            parameters=_parameter_records(raw, declarator, encoding),
            linkage=linkage,
            ast_facts=_subtree_facts(node),
            raw=raw,
            table=table,
            common=common,
            conditional_depth=conditional_depth,
        )

    def _declaration_symbols(
        self,
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        scopes: tuple[str, ...],
        conditional_depth: int,
    ) -> list[dict[str, Any]]:
        declarators: list[Node] = []
        for child in node.named_children:
            if child.type == "init_declarator":
                candidate = _field(child, "declarator")
                if candidate is None:
                    candidate = child
                declarators.append(candidate)
            elif child.type in DECLARATOR_NODE_TYPES:
                declarators.append(child)
        output: list[dict[str, Any]] = []
        seen_anchors: set[tuple[int, int]] = set()
        for declarator in declarators:
            name_data = _declarator_name(raw, declarator, encoding)
            if name_data is None:
                continue
            name, declared_name, name_node = name_data
            anchor_key = (name_node.start_byte, name_node.end_byte)
            if anchor_key in seen_anchors:
                continue
            seen_anchors.add(anchor_key)
            function_declarator = _first_descendant(
                declarator, {"function_declarator"}
            )
            kind = "FUNCTION" if function_declarator is not None else "GLOBAL_VARIABLE"
            qualified = (
                declared_name
                if "::" in declared_name
                else "::".join(scopes + (name,))
            )
            signature, signature_sha, truncated = normalized_text(
                raw[node.start_byte : node.end_byte],
                encoding,
                max_utf8_bytes=SIGNATURE_MAX_UTF8_BYTES,
            )
            storage = [
                _node_text(raw, candidate, encoding)
                for candidate in node.named_children
                if candidate.type == "storage_class_specifier"
            ]
            linkage = "INTERNAL" if "static" in storage else "UNSPECIFIED"
            output.append(
                self._symbol_record(
                    node=node,
                    name_node=name_node,
                    name=name,
                    qualified_name=qualified,
                    symbol_kind=kind,
                    role="DECLARATION",
                    signature=signature,
                    signature_sha256=signature_sha,
                    signature_truncated=truncated,
                    parameters=(
                        _parameter_records(raw, declarator, encoding)
                        if function_declarator is not None
                        else []
                    ),
                    linkage=linkage,
                    ast_facts={key: 0 for key in FACT_KEYS},
                    raw=raw,
                    table=table,
                    common=common,
                    conditional_depth=conditional_depth,
                )
            )
        return output

    def _type_symbol(
        self,
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        scopes: tuple[str, ...],
        conditional_depth: int,
    ) -> dict[str, Any] | None:
        name = _field(node, "name")
        if node.type == "type_definition":
            declarator = _field(node, "declarator")
            if declarator is not None:
                name = _last_name_descendant(declarator)
            if name is None:
                candidates = [
                    candidate
                    for candidate in node.named_children
                    if candidate.type in DECLARATOR_NODE_TYPES
                ]
                name = _last_name_descendant(candidates[-1]) if candidates else None
        if name is None:
            name = _last_name_descendant(node)
        if name is None or not _clean(name):
            return None
        symbol_name = _node_text(raw, name, encoding)
        qualified = "::".join(scopes + (symbol_name,))
        keyword = TYPE_NODE_KINDS[node.type].lower()
        signature = f"{keyword} {qualified}"
        return self._symbol_record(
            node=node,
            name_node=name,
            name=symbol_name,
            qualified_name=qualified,
            symbol_kind=TYPE_NODE_KINDS[node.type],
            role=(
                "DEFINITION"
                if node.type == "type_definition" or _field(node, "body") is not None
                else "DECLARATION"
            ),
            signature=signature,
            signature_sha256=sha256(signature.encode("utf-8")),
            signature_truncated=False,
            parameters=[],
            linkage="TYPE_SCOPE",
            ast_facts={key: 0 for key in FACT_KEYS},
            raw=raw,
            table=table,
            common=common,
            conditional_depth=conditional_depth,
        )

    def _macro_symbol(
        self,
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        scopes: tuple[str, ...],
        conditional_depth: int,
    ) -> dict[str, Any] | None:
        name = _field(node, "name")
        if name is None:
            name = _first_descendant(node, {"identifier"})
        if name is None:
            return None
        symbol_name = _node_text(raw, name, encoding)
        parameters_node = _field(node, "parameters")
        parameters: list[str] = []
        if parameters_node is not None:
            parameters = [
                _node_text(raw, child, encoding)
                for child in parameters_node.named_children
                if child.type in NAME_NODE_TYPES
            ]
        signature = f"#define {symbol_name}"
        if node.type == "preproc_function_def":
            signature += "(" + ",".join(parameters) + ")"
        return self._symbol_record(
            node=node,
            name_node=name,
            name=symbol_name,
            qualified_name="::".join(scopes + (symbol_name,)),
            symbol_kind="MACRO",
            role="DEFINITION",
            signature=signature,
            signature_sha256=sha256(signature.encode("utf-8")),
            signature_truncated=False,
            parameters=parameters,
            linkage="PREPROCESSOR",
            ast_facts={key: 0 for key in FACT_KEYS},
            raw=raw,
            table=table,
            common=common,
            conditional_depth=conditional_depth,
        )

    def _namespace_symbol(
        self,
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        scopes: tuple[str, ...],
        conditional_depth: int,
    ) -> dict[str, Any] | None:
        scope = _scope_name(raw, node, encoding)
        if scope is None:
            return None
        name, name_node = scope
        qualified = "::".join(scopes + (name,))
        signature = f"namespace {qualified}"
        return self._symbol_record(
            node=node,
            name_node=name_node,
            name=name,
            qualified_name=qualified,
            symbol_kind="NAMESPACE",
            role="DEFINITION",
            signature=signature,
            signature_sha256=sha256(signature.encode("utf-8")),
            signature_truncated=False,
            parameters=[],
            linkage="NAMESPACE_SCOPE",
            ast_facts={key: 0 for key in FACT_KEYS},
            raw=raw,
            table=table,
            common=common,
            conditional_depth=conditional_depth,
        )

    @staticmethod
    def _symbol_record(
        *,
        node: Node,
        name_node: Node,
        name: str,
        qualified_name: str,
        symbol_kind: str,
        role: str,
        signature: str,
        signature_sha256: str,
        signature_truncated: bool,
        parameters: list[str],
        linkage: str,
        ast_facts: dict[str, int],
        raw: bytes,
        table: ChunkTable,
        common: dict[str, Any],
        conditional_depth: int,
    ) -> dict[str, Any]:
        reference = table.reference(
            raw=raw,
            path=common["path"],
            git_blob_oid=common["git_blob_oid"],
            start=node.start_byte,
            end=node.end_byte,
            anchor_start=name_node.start_byte,
            anchor_end=name_node.end_byte,
        )
        identity_value = {
            "target_commit_sha": common["target_commit_sha"],
            "target_scope_sha256": common["target_scope_sha256"],
            "path": common["path"],
            "git_blob_oid": common["git_blob_oid"],
            "symbol_kind": symbol_kind,
            "role": role,
            "name": name,
            "qualified_name": qualified_name,
            "start_byte": node.start_byte,
            "end_byte_exclusive": node.end_byte,
            "source_span_sha256": reference["extent"]["source_span_sha256"],
        }
        identity = hashlib.sha256(
            SYMBOL_ID_DOMAIN + canonical_json(identity_value, newline=False)
        ).hexdigest()
        return {
            "schema_version": 1,
            **common,
            "symbol_id": f"S1-{identity[:24]}",
            "symbol_identity_sha256": identity,
            "name": name,
            "qualified_name": qualified_name,
            "symbol_kind": symbol_kind,
            "role": role,
            "signature": signature,
            "signature_sha256": signature_sha256,
            "signature_truncated": signature_truncated,
            "parameters": parameters,
            "parameter_count": len(parameters),
            "linkage": linkage,
            "conditional_compilation_depth": conditional_depth,
            "ast_facts": ast_facts,
            "reference": reference,
        }

    @staticmethod
    def _call_occurrence(
        *,
        node: Node,
        caller_symbol_id: str,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        conditional_depth: int,
    ) -> dict[str, Any] | None:
        function = _field(node, "function")
        if function is None or function.end_byte <= function.start_byte:
            return None
        expression, expression_sha, expression_truncated = normalized_text(
            raw[function.start_byte : function.end_byte],
            encoding,
            max_utf8_bytes=256,
        )
        name_node = _last_name_descendant(function)
        callee_name = (
            _node_text(raw, name_node, encoding) if name_node is not None else None
        )
        if function.type in {"identifier", "qualified_identifier", "scoped_identifier"}:
            syntactic_kind = (
                "QUALIFIED" if "::" in expression else "DIRECT_IDENTIFIER"
            )
        elif function.type == "field_expression":
            syntactic_kind = "MEMBER"
        else:
            syntactic_kind = "INDIRECT"
        anchor_start = (
            name_node.start_byte if name_node is not None else function.start_byte
        )
        anchor_end = (
            name_node.end_byte if name_node is not None else function.end_byte
        )
        reference = table.reference(
            raw=raw,
            path=common["path"],
            git_blob_oid=common["git_blob_oid"],
            start=node.start_byte,
            end=node.end_byte,
            anchor_start=anchor_start,
            anchor_end=anchor_end,
        )
        identity_value = {
            "target_commit_sha": common["target_commit_sha"],
            "target_scope_sha256": common["target_scope_sha256"],
            "path": common["path"],
            "git_blob_oid": common["git_blob_oid"],
            "caller_symbol_id": caller_symbol_id,
            "callee_expression": expression,
            "start_byte": node.start_byte,
            "end_byte_exclusive": node.end_byte,
            "source_span_sha256": reference["extent"]["source_span_sha256"],
        }
        identity = hashlib.sha256(
            CALL_ID_DOMAIN + canonical_json(identity_value, newline=False)
        ).hexdigest()
        return {
            "schema_version": 1,
            **common,
            "call_id": f"E1-{identity[:24]}",
            "call_identity_sha256": identity,
            "caller_symbol_id": caller_symbol_id,
            "callee_name": callee_name,
            "callee_expression": expression,
            "callee_expression_sha256": expression_sha,
            "callee_expression_truncated": expression_truncated,
            "syntactic_kind": syntactic_kind,
            "conditional_compilation_depth": conditional_depth,
            "candidate_definition_ids": [],
            "declaration_candidate_ids": [],
            "resolution": "PENDING",
            "reference": reference,
        }

    @staticmethod
    def _include_edge(
        *,
        node: Node,
        raw: bytes,
        encoding: str,
        table: ChunkTable,
        common: dict[str, Any],
        conditional_depth: int,
    ) -> dict[str, Any] | None:
        target = _field(node, "path")
        if target is None:
            target = _first_descendant(node, {"string_literal", "system_lib_string"})
        if target is None:
            return None
        spelling = _node_text(raw, target, encoding)
        normalized = spelling.strip('<>"')
        reference = table.reference(
            raw=raw,
            path=common["path"],
            git_blob_oid=common["git_blob_oid"],
            start=node.start_byte,
            end=node.end_byte,
            anchor_start=target.start_byte,
            anchor_end=target.end_byte,
        )
        identity_value = {
            "target_commit_sha": common["target_commit_sha"],
            "target_scope_sha256": common["target_scope_sha256"],
            "path": common["path"],
            "git_blob_oid": common["git_blob_oid"],
            "include_spelling": spelling,
            "start_byte": node.start_byte,
            "end_byte_exclusive": node.end_byte,
            "source_span_sha256": reference["extent"]["source_span_sha256"],
        }
        identity = hashlib.sha256(
            INCLUDE_ID_DOMAIN + canonical_json(identity_value, newline=False)
        ).hexdigest()
        return {
            "schema_version": 1,
            **common,
            "include_id": f"I1-{identity[:24]}",
            "include_identity_sha256": identity,
            "include_spelling": spelling,
            "normalized_target": normalized,
            "conditional_compilation_depth": conditional_depth,
            "resolution": "PENDING",
            "resolved_paths": [],
            "reference": reference,
        }
