"""Turn parser spans into content-addressed Chunk manifest records."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .batcher import render_evidence_frame
from .parser import ParsedFile
from .tokenizer import TOKEN_COUNT_KIND, TOKEN_COUNTER, rendered_slice


CHUNK_ID_DOMAIN = b"ai-sast-chunk-id-v1\0"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _line_and_column(raw: bytes, offset: int) -> tuple[int, int]:
    line = raw.count(b"\n", 0, offset) + 1
    previous = raw.rfind(b"\n", 0, offset)
    column = offset if previous < 0 else offset - previous - 1
    return line, column


def build_file_chunks(
    *,
    source_record: dict[str, Any],
    raw: bytes,
    parsed: ParsedFile,
    target_commit_sha: str,
    target_scope_sha256: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for ordinal, span in enumerate(parsed.spans):
        content = raw[span.start_byte : span.end_byte]
        raw_sha256 = hashlib.sha256(content).hexdigest()
        rendered = rendered_slice(content, parsed.source_encoding)
        rendered_bytes = rendered.encode("utf-8")
        identity_value = {
            "target_commit_sha": target_commit_sha,
            "target_scope_sha256": target_scope_sha256,
            "path": source_record["path"],
            "git_blob_oid": source_record["git_blob_oid"],
            "kind": span.kind,
            "start_byte": span.start_byte,
            "end_byte_exclusive": span.end_byte,
            "raw_content_sha256": raw_sha256,
        }
        identity_sha256 = hashlib.sha256(
            CHUNK_ID_DOMAIN + canonical_json(identity_value)
        ).hexdigest()
        start_line, start_column = _line_and_column(raw, span.start_byte)
        end_line, _ = _line_and_column(raw, span.end_byte - 1)
        end_point_line, end_column = _line_and_column(raw, span.end_byte)
        record: dict[str, Any] = {
            "schema_version": 1,
            "target_commit_sha": target_commit_sha,
            "target_scope_sha256": target_scope_sha256,
            "chunk_id": f"C1-{identity_sha256[:24]}",
            "chunk_identity_sha256": identity_sha256,
            "path": source_record["path"],
            "git_blob_oid": source_record["git_blob_oid"],
            "language": parsed.language,
            "source_encoding": parsed.source_encoding,
            "parse_mode": parsed.outcome,
            "kind": span.kind,
            "node_type": span.node_type,
            "parent_kind": span.parent_kind,
            "split_mode": span.split_mode,
            "structural_kinds": list(span.structural_kinds),
            "node_types": list(span.node_types),
            "structural_unit_start_byte": span.structural_unit_start_byte,
            "structural_unit_end_byte_exclusive": span.structural_unit_end_byte,
            "structural_unit_kind": span.structural_unit_kind,
            "file_chunk_ordinal": ordinal,
            "start_byte": span.start_byte,
            "end_byte_exclusive": span.end_byte,
            "start_line": start_line,
            "start_column_byte": start_column,
            "end_line": end_line,
            "end_point_line": end_point_line,
            "end_column_byte_exclusive": end_column,
            "raw_content_sha256": raw_sha256,
            "raw_size_bytes": len(content),
            "rendered_content_sha256": hashlib.sha256(rendered_bytes).hexdigest(),
            "rendered_content_utf8_bytes": len(rendered_bytes),
            "token_count_kind": TOKEN_COUNT_KIND,
            "token_counter": TOKEN_COUNTER,
        }
        frame = render_evidence_frame(record, rendered)
        record["evidence_frame_sha256"] = hashlib.sha256(frame).hexdigest()
        record["evidence_frame_utf8_bytes"] = len(frame)
        record["token_count"] = len(frame)
        records.append(record)
    return records
