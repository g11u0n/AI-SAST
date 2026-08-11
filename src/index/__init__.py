"""Deterministic Compact Code Index.

The authoritative index is a set of canonical JSON/JSONL artifacts.  Runtime
caches may be built from those artifacts, but they are never evidence.
"""

from .code_index import CodeIndex, ChunkTable, canonical_json, canonical_jsonl
from .relation_index import resolve_call_edges
from .symbol_index import FileExtraction, SymbolExtractor

__all__ = [
    "ChunkTable",
    "CodeIndex",
    "FileExtraction",
    "SymbolExtractor",
    "canonical_json",
    "canonical_jsonl",
    "resolve_call_edges",
]
