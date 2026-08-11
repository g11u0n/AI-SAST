"""Canonical serialization, source grounding, and read-only index access."""

from __future__ import annotations

import bisect
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


def canonical_json(value: Any, *, newline: bool = True) -> bytes:
    """Serialize one canonical JSON value as UTF-8 without a BOM."""

    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return raw + (b"\n" if newline else b"")


def canonical_jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json(record) for record in records)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def line_and_column(raw: bytes, offset: int) -> tuple[int, int]:
    if offset < 0 or offset > len(raw):
        raise ValueError("Source offset is outside the Git blob")
    line = raw.count(b"\n", 0, offset) + 1
    previous = raw.rfind(b"\n", 0, offset)
    column = offset if previous < 0 else offset - previous - 1
    return line, column


def normalized_text(raw: bytes, encoding: str, *, max_utf8_bytes: int) -> tuple[str, str, bool]:
    """Return whitespace-normalized display text, its full hash, and truncation."""

    decoded = raw.decode(encoding, "strict")
    full = " ".join(decoded.split())
    full_bytes = full.encode("utf-8")
    digest = sha256(full_bytes)
    if len(full_bytes) <= max_utf8_bytes:
        return full, digest, False
    output: list[str] = []
    used = 0
    for character in full:
        encoded = character.encode("utf-8")
        if used + len(encoded) > max_utf8_bytes:
            break
        output.append(character)
        used += len(encoded)
    return "".join(output), digest, True


@dataclass(frozen=True)
class ChunkTable:
    """The exact Phase 2 partition for one source file."""

    chunks: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        if not self.chunks:
            raise ValueError("A non-empty source file needs at least one Chunk")
        cursor = 0
        for chunk in self.chunks:
            if chunk["start_byte"] != cursor:
                raise ValueError("ChunkTable is not an exact source partition")
            if chunk["end_byte_exclusive"] <= chunk["start_byte"]:
                raise ValueError("ChunkTable contains an empty range")
            cursor = chunk["end_byte_exclusive"]
        object.__setattr__(
            self,
            "_starts",
            tuple(chunk["start_byte"] for chunk in self.chunks),
        )

    @property
    def size_bytes(self) -> int:
        return self.chunks[-1]["end_byte_exclusive"]

    def containing(self, offset: int) -> dict[str, Any]:
        if offset == self.size_bytes and self.size_bytes:
            offset -= 1
        position = bisect.bisect_right(self._starts, offset) - 1
        if position < 0:
            raise ValueError("No Chunk contains the requested byte")
        chunk = self.chunks[position]
        if not chunk["start_byte"] <= offset < chunk["end_byte_exclusive"]:
            raise ValueError("No Chunk contains the requested byte")
        return chunk

    def intersecting(self, start: int, end: int) -> tuple[dict[str, Any], ...]:
        if start < 0 or end <= start or end > self.size_bytes:
            raise ValueError("Invalid source span")
        position = max(0, bisect.bisect_right(self._starts, start) - 1)
        matches: list[dict[str, Any]] = []
        for chunk in self.chunks[position:]:
            if chunk["start_byte"] >= end:
                break
            if chunk["end_byte_exclusive"] > start:
                matches.append(chunk)
        if not matches:
            raise ValueError("Source span does not intersect a Chunk")
        return tuple(matches)

    def reference(
        self,
        *,
        raw: bytes,
        path: str,
        git_blob_oid: str,
        start: int,
        end: int,
        anchor_start: int,
        anchor_end: int,
    ) -> dict[str, Any]:
        if len(raw) != self.size_bytes:
            raise ValueError("Chunk partition and Git blob size differ")
        if not start <= anchor_start < anchor_end <= end:
            raise ValueError("Reference anchor is outside its source span")
        anchor = self.containing(anchor_start)
        intersections = self.intersecting(start, end)
        start_line, start_column = line_and_column(raw, start)
        end_line, _ = line_and_column(raw, end - 1)
        end_point_line, end_column = line_and_column(raw, end)
        anchor_start_line, anchor_start_column = line_and_column(raw, anchor_start)
        anchor_end_line, _ = line_and_column(raw, anchor_end - 1)
        anchor_end_point_line, anchor_end_column = line_and_column(raw, anchor_end)
        return {
            "path": path,
            "git_blob_oid": git_blob_oid,
            "anchor": {
                "start_byte": anchor_start,
                "end_byte_exclusive": anchor_end,
                "start_line": anchor_start_line,
                "start_column_byte": anchor_start_column,
                "end_line": anchor_end_line,
                "end_point_line": anchor_end_point_line,
                "end_column_byte_exclusive": anchor_end_column,
                "source_span_sha256": sha256(raw[anchor_start:anchor_end]),
                "chunk_id": anchor["chunk_id"],
                "chunk_content_sha256": anchor["raw_content_sha256"],
            },
            "extent": {
                "start_byte": start,
                "end_byte_exclusive": end,
                "start_line": start_line,
                "start_column_byte": start_column,
                "end_line": end_line,
                "end_point_line": end_point_line,
                "end_column_byte_exclusive": end_column,
                "source_span_sha256": sha256(raw[start:end]),
                "first_chunk_id": intersections[0]["chunk_id"],
                "last_chunk_id": intersections[-1]["chunk_id"],
                "chunk_count": len(intersections),
            },
        }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith("\n") or "\r" in line:
                raise ValueError(f"Non-canonical JSONL line at {path}:{line_number}")
            if not line.strip():
                raise ValueError(f"Blank JSONL line at {path}:{line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record is not an object at {path}:{line_number}")
            if line.encode("utf-8") != canonical_json(value):
                raise ValueError(f"Non-canonical JSONL object at {path}:{line_number}")
            records.append(value)
    return records


class CodeIndex:
    """Read-only, exact-match view used by Phase 3 canaries and Phase 4 tools."""

    def __init__(
        self,
        *,
        symbols: Sequence[dict[str, Any]],
        calls: Sequence[dict[str, Any]],
        files: Sequence[dict[str, Any]],
        chunk_facts: Sequence[dict[str, Any]],
    ) -> None:
        self.symbols = tuple(symbols)
        self.calls = tuple(calls)
        self.files = tuple(files)
        self.chunk_facts = tuple(chunk_facts)
        by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_id: dict[str, dict[str, Any]] = {}
        for symbol in self.symbols:
            by_name[symbol["name"]].append(symbol)
            if symbol["qualified_name"] != symbol["name"]:
                by_name[symbol["qualified_name"]].append(symbol)
            if symbol["symbol_id"] in by_id:
                raise ValueError(f"Duplicate Symbol ID: {symbol['symbol_id']}")
            by_id[symbol["symbol_id"]] = symbol
        self._symbols_by_name = {
            key: tuple(sorted(values, key=_symbol_sort_key))
            for key, values in by_name.items()
        }
        self._symbols_by_id = by_id
        outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for call in self.calls:
            caller = call["caller_symbol_id"]
            outgoing[caller].append(call)
            for target in call["candidate_definition_ids"]:
                incoming[target].append(call)
        self._outgoing = {
            key: tuple(sorted(values, key=_call_sort_key))
            for key, values in outgoing.items()
        }
        self._incoming = {
            key: tuple(sorted(values, key=_call_sort_key))
            for key, values in incoming.items()
        }

    @classmethod
    def from_directory(cls, directory: Path) -> "CodeIndex":
        return cls(
            symbols=load_jsonl(directory / "symbol_index.jsonl"),
            calls=load_jsonl(directory / "call_edges.jsonl"),
            files=load_jsonl(directory / "file_index.jsonl"),
            chunk_facts=load_jsonl(directory / "chunk_facts.jsonl"),
        )

    def search_exact(self, symbol: str) -> tuple[dict[str, Any], ...]:
        return self._symbols_by_name.get(symbol, ())

    def symbol(self, symbol_id: str) -> dict[str, Any] | None:
        return self._symbols_by_id.get(symbol_id)

    def outgoing(self, symbol_id: str) -> tuple[dict[str, Any], ...]:
        return self._outgoing.get(symbol_id, ())

    def incoming(self, symbol_id: str) -> tuple[dict[str, Any], ...]:
        return self._incoming.get(symbol_id, ())


def _symbol_sort_key(record: dict[str, Any]) -> tuple[bytes, int, str, str]:
    return (
        record["reference"]["path"].encode("utf-8"),
        record["reference"]["extent"]["start_byte"],
        record["symbol_kind"],
        record["symbol_identity_sha256"],
    )


def _call_sort_key(record: dict[str, Any]) -> tuple[bytes, int, str]:
    return (
        record["reference"]["path"].encode("utf-8"),
        record["reference"]["extent"]["start_byte"],
        record["call_identity_sha256"],
    )


def iter_file_chunks(
    chunks: Iterable[dict[str, Any]],
) -> Iterator[tuple[str, tuple[dict[str, Any], ...]]]:
    current_path: str | None = None
    current: list[dict[str, Any]] = []
    for chunk in chunks:
        path = chunk["path"]
        if current_path is not None and path != current_path:
            yield current_path, tuple(current)
            current.clear()
        current_path = path
        current.append(chunk)
    if current_path is not None:
        yield current_path, tuple(current)
