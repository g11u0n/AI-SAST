"""Exact evidence rendering and deterministic next-fit Batch construction."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from .tokenizer import TOKEN_COUNT_KIND, TOKEN_COUNTER


BATCH_ID_DOMAIN = b"ai-sast-batch-id-v1\0"
RENDERER_VERSION = "evidence-frame-v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def render_evidence_frame(record: dict[str, Any], rendered_content: str) -> bytes:
    header = {
        "chunk_id": record["chunk_id"],
        "content_sha256": record["raw_content_sha256"],
        "end_line": record["end_line"],
        "kind": record["kind"],
        "path": record["path"],
        "start_line": record["start_line"],
    }
    return (
        b"@@AI_SAST_CHUNK_V1 "
        + _canonical_json(header)
        + b"\n<<<CODE\n"
        + rendered_content.encode("utf-8")
        + b"\nCODE>>>\n"
    )


def build_batches(
    *,
    chunks: list[dict[str, Any]],
    content_loader: Callable[[dict[str, Any]], str],
    target_commit_sha: str,
    target_scope_sha256: str,
    source_manifest_sha256: str,
    chunking_contract_sha256: str,
    file_manifest_sha256: str,
    chunk_manifest_sha256: str,
    max_payload_utf8_bytes: int,
) -> list[dict[str, Any]]:
    packed: list[tuple[list[dict[str, Any]], bytes]] = []
    current_records: list[dict[str, Any]] = []
    current_payload = b""
    for chunk in chunks:
        frame = render_evidence_frame(chunk, content_loader(chunk))
        if hashlib.sha256(frame).hexdigest() != chunk["evidence_frame_sha256"]:
            raise ValueError(f"Evidence frame drift for {chunk['chunk_id']}")
        if len(frame) != chunk["evidence_frame_utf8_bytes"]:
            raise ValueError(f"Evidence frame size drift for {chunk['chunk_id']}")
        if len(frame) > max_payload_utf8_bytes:
            raise ValueError(f"Chunk cannot fit the locked Batch budget: {chunk['chunk_id']}")
        if current_records and len(current_payload) + len(frame) > max_payload_utf8_bytes:
            packed.append((current_records, current_payload))
            current_records = []
            current_payload = b""
        current_records.append(chunk)
        current_payload += frame
    if current_records:
        packed.append((current_records, current_payload))

    batches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ordinal, (records, payload) in enumerate(packed):
        chunk_ids = [record["chunk_id"] for record in records]
        ordered_chunk_identity_sha256 = hashlib.sha256(
            b"\0".join(record["chunk_identity_sha256"].encode("ascii") for record in records)
        ).hexdigest()
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        identity_value = {
            "target_commit_sha": target_commit_sha,
            "target_scope_sha256": target_scope_sha256,
            "budget_counter": TOKEN_COUNTER,
            "max_payload_utf8_bytes": max_payload_utf8_bytes,
            "ordered_chunk_identity_sha256": ordered_chunk_identity_sha256,
            "payload_sha256": payload_sha256,
            "payload_utf8_bytes": len(payload),
        }
        identity_sha256 = hashlib.sha256(
            BATCH_ID_DOMAIN + _canonical_json(identity_value)
        ).hexdigest()
        batch_id = f"B1-{identity_sha256[:24]}"
        if batch_id in seen_ids:
            raise ValueError(f"Truncated Batch ID collision: {batch_id}")
        seen_ids.add(batch_id)
        batches.append(
            {
                "schema_version": 1,
                "target_commit_sha": target_commit_sha,
                "target_scope_sha256": target_scope_sha256,
                "source_manifest_sha256": source_manifest_sha256,
                "chunking_contract_sha256": chunking_contract_sha256,
                "file_manifest_sha256": file_manifest_sha256,
                "chunk_manifest_sha256": chunk_manifest_sha256,
                "batch_id": batch_id,
                "batch_identity_sha256": identity_sha256,
                "batch_ordinal": ordinal,
                "renderer_version": RENDERER_VERSION,
                "budget_counter": TOKEN_COUNTER,
                "budget_unit_rule": "one_budget_unit_per_utf8_byte",
                "max_payload_utf8_bytes": max_payload_utf8_bytes,
                "chunk_ids": chunk_ids,
                "chunk_count": len(chunk_ids),
                "ordered_chunk_identity_sha256": ordered_chunk_identity_sha256,
                "payload_sha256": payload_sha256,
                "payload_utf8_bytes": len(payload),
                "token_count": len(payload),
                "token_count_kind": TOKEN_COUNT_KIND,
            }
        )
    return batches
