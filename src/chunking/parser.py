"""Tree-sitter parser selection and non-overlapping source partitioning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

from .tokenizer import deterministic_windows


STRUCTURAL_KINDS = {
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
    "namespace_definition": "NAMESPACE",
    "template_declaration": "TEMPLATE",
    "linkage_specification": "LINKAGE",
}

TRANSPARENT_CONTAINERS = {
    "translation_unit",
    "preproc_if",
    "preproc_ifdef",
    "preproc_ifndef",
    "preproc_elif",
    "preproc_else",
    "namespace_definition",
    "declaration_list",
    "linkage_specification",
}

PROTECTED_STRUCTURAL_KINDS = {
    "FUNCTION",
    "TYPE",
    "MACRO",
    "DECLARATION",
    "TEMPLATE",
    "LINE_WINDOW",
}


@dataclass(frozen=True)
class ParseScore:
    error_byte_union: int
    missing_count: int
    error_count: int
    grammar_tiebreak: int

    def as_list(self) -> list[int]:
        return [
            self.error_byte_union,
            self.missing_count,
            self.error_count,
            self.grammar_tiebreak,
        ]


@dataclass(frozen=True)
class ParsedSpan:
    start_byte: int
    end_byte: int
    kind: str
    node_type: str
    parent_kind: str
    split_mode: str
    structural_kinds: tuple[str, ...]
    node_types: tuple[str, ...]
    structural_unit_start_byte: int
    structural_unit_end_byte: int
    structural_unit_kind: str


@dataclass(frozen=True)
class ParsedFile:
    outcome: str
    language: str
    grammar: str
    source_encoding: str
    spans: tuple[ParsedSpan, ...]
    selected_score: ParseScore | None
    parser_attempts: tuple[dict[str, Any], ...]
    fallback_reason: str | None


def _walk_nodes(root: Node) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _parse_score(root: Node, grammar_tiebreak: int) -> ParseScore:
    error_ranges: list[tuple[int, int]] = []
    missing_count = 0
    error_count = 0
    for node in _walk_nodes(root):
        if node.is_error:
            error_count += 1
            if node.end_byte > node.start_byte:
                error_ranges.append((node.start_byte, node.end_byte))
        if node.is_missing:
            missing_count += 1
    union = 0
    cursor = -1
    for start, end in sorted(error_ranges):
        if end <= cursor:
            continue
        if start >= cursor:
            union += end - start
        else:
            union += end - cursor
        cursor = max(cursor, end)
    return ParseScore(union, missing_count, error_count, grammar_tiebreak)


def _kind_for_node(node_type: str) -> str:
    if node_type in STRUCTURAL_KINDS:
        return STRUCTURAL_KINDS[node_type]
    if node_type.startswith("preproc_"):
        return "PREPROCESSOR"
    return "AST_BLOCK"


def _node_has_diagnostic(root: Node) -> bool:
    return any(node.is_error or node.is_missing for node in _walk_nodes(root))


class StructuralParser:
    """Select a grammar deterministically and partition the complete raw blob."""

    def __init__(self, *, max_source_utf8_bytes: int = 3000) -> None:
        self.max_source_utf8_bytes = max_source_utf8_bytes
        self.languages = {
            "c": Language(tree_sitter_c.language()),
            "cpp": Language(tree_sitter_cpp.language()),
        }
        self.parsers = {
            name: Parser(language) for name, language in self.languages.items()
        }

    def parse(self, raw: bytes, *, extension: str, source_encoding: str) -> ParsedFile:
        if not raw:
            return ParsedFile(
                outcome="FALLBACK_SUCCESS",
                language="cpp" if extension == ".cpp" else "c",
                grammar="line-window",
                source_encoding=source_encoding,
                spans=(),
                selected_score=None,
                parser_attempts=(),
                fallback_reason="empty_blob",
            )
        if source_encoding != "utf-8":
            language = "cpp" if extension == ".cpp" else "c"
            return self._fallback(
                raw,
                language=language,
                source_encoding=source_encoding,
                reason="non_utf8_source",
                attempts=(),
            )

        candidates = ["cpp"] if extension == ".cpp" else ["c"]
        if extension == ".h":
            candidates = ["c", "cpp"]
        attempts: list[tuple[str, Any, ParseScore]] = []
        attempt_records: list[dict[str, Any]] = []
        for grammar_tiebreak, language in enumerate(candidates):
            try:
                tree = self.parsers[language].parse(raw, encoding="utf8")
                score = _parse_score(tree.root_node, grammar_tiebreak)
                attempts.append((language, tree, score))
                attempt_records.append(
                    {
                        "grammar": language,
                        "status": "OK",
                        "score": score.as_list(),
                    }
                )
            except Exception as exc:  # pragma: no cover - native parser guard
                attempt_records.append(
                    {
                        "grammar": language,
                        "status": "ERROR",
                        "error_type": type(exc).__name__,
                    }
                )
        if not attempts:
            return self._fallback(
                raw,
                language=candidates[0],
                source_encoding=source_encoding,
                reason="all_parser_attempts_failed",
                attempts=tuple(attempt_records),
            )
        language, tree, score = min(
            attempts,
            key=lambda item: (
                item[2].error_byte_union,
                item[2].missing_count,
                item[2].error_count,
                item[2].grammar_tiebreak,
            ),
        )
        if score.error_count or score.missing_count or tree.root_node.has_error:
            spans = self._structural_spans(
                raw, tree.root_node, fallback_diagnostics=True
            )
            if spans:
                self._assert_partition(spans, len(raw))
                return ParsedFile(
                    outcome="FALLBACK_SUCCESS",
                    language=language,
                    grammar=f"tree-sitter-{language}+line-window",
                    source_encoding=source_encoding,
                    spans=tuple(spans),
                    selected_score=score,
                    parser_attempts=tuple(attempt_records),
                    fallback_reason="syntax_error_or_missing_node",
                )
            return self._fallback(
                raw,
                language=language,
                source_encoding=source_encoding,
                reason="syntax_error_or_missing_node",
                attempts=tuple(attempt_records),
                selected_score=score,
            )
        spans = self._structural_spans(raw, tree.root_node)
        if not spans:
            return self._fallback(
                raw,
                language=language,
                source_encoding=source_encoding,
                reason="no_structural_nodes",
                attempts=tuple(attempt_records),
                selected_score=score,
            )
        self._assert_partition(spans, len(raw))
        return ParsedFile(
            outcome="PARSE_SUCCESS",
            language=language,
            grammar=f"tree-sitter-{language}",
            source_encoding=source_encoding,
            spans=tuple(spans),
            selected_score=score,
            parser_attempts=tuple(attempt_records),
            fallback_reason=None,
        )

    def _fallback(
        self,
        raw: bytes,
        *,
        language: str,
        source_encoding: str,
        reason: str,
        attempts: tuple[dict[str, Any], ...],
        selected_score: ParseScore | None = None,
    ) -> ParsedFile:
        windows = deterministic_windows(
            raw,
            encoding=source_encoding,
            max_rendered_utf8_bytes=self.max_source_utf8_bytes,
        )
        spans = tuple(
            ParsedSpan(
                start,
                end,
                "LINE_WINDOW",
                "line_window",
                "LINE_WINDOW",
                "FALLBACK",
                ("LINE_WINDOW",),
                ("line_window",),
                start,
                end,
                "LINE_WINDOW",
            )
            for start, end in windows
        )
        self._assert_partition(spans, len(raw))
        return ParsedFile(
            outcome="FALLBACK_SUCCESS",
            language=language,
            grammar="line-window",
            source_encoding=source_encoding,
            spans=spans,
            selected_score=selected_score,
            parser_attempts=attempts,
            fallback_reason=reason,
        )

    def _structural_spans(
        self, raw: bytes, root: Node, *, fallback_diagnostics: bool = False
    ) -> list[ParsedSpan]:
        top_level = self._transparent_units(root, 0, len(raw))
        result: list[ParsedSpan] = []
        for start, end, node in top_level:
            root_kind = _kind_for_node(node.type)
            if fallback_diagnostics and _node_has_diagnostic(node):
                result.extend(
                    self._line_window_spans(
                        raw,
                        start=start,
                        end=end,
                        parent_kind=root_kind,
                        node_type=node.type,
                        split_mode="DIAGNOSTIC_FALLBACK",
                    )
                )
            else:
                result.extend(
                    self._split_node(
                        raw,
                        node,
                        start,
                        end,
                        root_kind,
                        structural_unit_start=start,
                        structural_unit_end=end,
                        fragmented=False,
                    )
                )
        return self._coalesce_spans(result)

    def _transparent_units(
        self, node: Node, outer_start: int, outer_end: int
    ) -> list[tuple[int, int, Node]]:
        if node.type not in TRANSPARENT_CONTAINERS:
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
            child_start = cursor
            child_end = child.end_byte
            pieces.extend(self._transparent_units(child, child_start, child_end))
            cursor = child_end
        if cursor < outer_end and pieces:
            start, _, last = pieces[-1]
            pieces[-1] = (start, outer_end, last)
        return pieces

    def _split_node(
        self,
        raw: bytes,
        node: Node,
        outer_start: int,
        outer_end: int,
        parent_kind: str,
        *,
        structural_unit_start: int,
        structural_unit_end: int,
        fragmented: bool,
    ) -> list[ParsedSpan]:
        rendered_size = len(raw[outer_start:outer_end])
        if rendered_size <= self.max_source_utf8_bytes:
            chunk_kind = parent_kind if not fragmented else _kind_for_node(node.type)
            if fragmented and chunk_kind in {"DECLARATION", "PREPROCESSOR"}:
                chunk_kind = "AST_BLOCK"
            structural_kinds = tuple(dict.fromkeys((parent_kind, chunk_kind)))
            return [
                ParsedSpan(
                    outer_start,
                    outer_end,
                    chunk_kind,
                    node.type,
                    parent_kind,
                    "STRUCTURAL" if not fragmented else "AST_CHILD",
                    structural_kinds,
                    (node.type,),
                    structural_unit_start,
                    structural_unit_end,
                    parent_kind,
                )
            ]
        children = [
            child
            for child in node.named_children
            if child.end_byte > child.start_byte
            and child.start_byte >= outer_start
            and child.end_byte <= outer_end
        ]
        pieces: list[tuple[int, int, Node]] = []
        cursor = outer_start
        for child in children:
            if child.start_byte < cursor:
                continue
            pieces.append((cursor, child.end_byte, child))
            cursor = child.end_byte
        if cursor < outer_end and pieces:
            start, _, last_node = pieces[-1]
            pieces[-1] = (start, outer_end, last_node)
        if len(pieces) >= 2 and all(end > start for start, end, _ in pieces):
            result: list[ParsedSpan] = []
            for start, end, child in pieces:
                result.extend(
                    self._split_node(
                        raw,
                        child,
                        start,
                        end,
                        parent_kind,
                        structural_unit_start=structural_unit_start,
                        structural_unit_end=structural_unit_end,
                        fragmented=True,
                    )
                )
            return result
        return self._line_window_spans(
            raw,
            start=outer_start,
            end=outer_end,
            parent_kind=parent_kind,
            node_type=node.type,
            split_mode="HARD_WINDOW",
            structural_unit_start=structural_unit_start,
            structural_unit_end=structural_unit_end,
        )

    def _line_window_spans(
        self,
        raw: bytes,
        *,
        start: int,
        end: int,
        parent_kind: str,
        node_type: str,
        split_mode: str,
        structural_unit_start: int | None = None,
        structural_unit_end: int | None = None,
    ) -> list[ParsedSpan]:
        unit_start = start if structural_unit_start is None else structural_unit_start
        unit_end = end if structural_unit_end is None else structural_unit_end
        windows = deterministic_windows(
            raw[start:end],
            encoding="utf-8",
            max_rendered_utf8_bytes=self.max_source_utf8_bytes,
        )
        return [
            ParsedSpan(
                start + local_start,
                start + local_end,
                "LINE_WINDOW" if split_mode == "DIAGNOSTIC_FALLBACK" else "AST_BLOCK",
                node_type,
                parent_kind,
                split_mode,
                tuple(dict.fromkeys((parent_kind, "LINE_WINDOW" if split_mode == "DIAGNOSTIC_FALLBACK" else "AST_BLOCK"))),
                (node_type,),
                unit_start,
                unit_end,
                parent_kind,
            )
            for local_start, local_end in windows
        ]

    def _coalesce_spans(self, spans: list[ParsedSpan]) -> list[ParsedSpan]:
        """Pack adjacent structural units while retaining their structural metadata."""

        result: list[ParsedSpan] = []
        for span in spans:
            previous = result[-1] if result else None
            same_unit = bool(
                previous
                and previous.structural_unit_start_byte == span.structural_unit_start_byte
                and previous.structural_unit_end_byte == span.structural_unit_end_byte
                and previous.structural_unit_kind == span.structural_unit_kind
            )
            cross_unit_allowed = bool(
                previous
                and not (
                    set(previous.structural_kinds + span.structural_kinds)
                    & PROTECTED_STRUCTURAL_KINDS
                )
            )
            if (
                previous
                and previous.end_byte == span.start_byte
                and span.end_byte - previous.start_byte <= self.max_source_utf8_bytes
                and (same_unit or cross_unit_allowed)
            ):
                previous = result.pop()
                kinds = tuple(dict.fromkeys(previous.structural_kinds + span.structural_kinds))
                node_types = tuple(dict.fromkeys(previous.node_types + span.node_types))
                if same_unit:
                    merged_kind = "AST_BLOCK"
                    merged_node_type = "structural_unit_fragment_group"
                    merged_parent = previous.structural_unit_kind
                    merged_mode = "COALESCED_WITHIN_UNIT"
                    unit_start = previous.structural_unit_start_byte
                    unit_end = previous.structural_unit_end_byte
                    unit_kind = previous.structural_unit_kind
                else:
                    merged_kind = "STRUCTURAL_GROUP"
                    merged_node_type = "translation_unit_group"
                    merged_parent = "STRUCTURAL_GROUP"
                    merged_mode = "COALESCED_UNPROTECTED"
                    unit_start = previous.structural_unit_start_byte
                    unit_end = span.structural_unit_end_byte
                    unit_kind = "STRUCTURAL_GROUP"
                result.append(
                    ParsedSpan(
                        previous.start_byte,
                        span.end_byte,
                        merged_kind,
                        merged_node_type,
                        merged_parent,
                        merged_mode,
                        kinds,
                        node_types,
                        unit_start,
                        unit_end,
                        unit_kind,
                    )
                )
            else:
                result.append(span)
        return result

    @staticmethod
    def _assert_partition(spans: Iterable[ParsedSpan], raw_size: int) -> None:
        cursor = 0
        for span in spans:
            if span.start_byte != cursor or span.end_byte <= span.start_byte:
                raise ValueError("Parser spans are not an exact non-overlapping partition")
            cursor = span.end_byte
        if cursor != raw_size:
            raise ValueError("Parser spans do not cover the complete Git blob")
