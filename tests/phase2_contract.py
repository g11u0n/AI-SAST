#!/usr/bin/env python3
"""Positive and negative contract tests for deterministic Phase 2 artifacts."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.chunking import StructuralParser, build_batches, build_file_chunks
from src.chunking.blob_source import GitBlobSource
from src.chunking.tokenizer import decode_source, deterministic_windows, rendered_slice


def load_script(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = load_script("phase2_verifier", "scripts/verify_phase2.py")
BUILD = load_script("phase2_builder", "scripts/build_phase2.py")


def source_record(path: str, raw: bytes, extension: str = ".c") -> dict[str, Any]:
    import hashlib

    oid = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
    return {
        "path": path,
        "git_blob_oid": oid,
        "mode": "100644",
        "size_bytes": len(raw),
        "extension": extension,
        "classification": "IN_SCOPE",
        "reason": "fixture",
    }


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(VERIFY.canonical_json(value))


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(VERIFY.canonical_json(value) for value in values))


class StructuralUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = StructuralParser(max_source_utf8_bytes=3000)

    def test_c_structural_parse_and_exact_partition(self) -> None:
        raw = (
            b"#define LIMIT 4\n"
            b"typedef struct Item { int value; } Item;\n"
            b"int add(int a, int b) { return a + b; }\n"
        )
        parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
        self.assertEqual(parsed.outcome, "PARSE_SUCCESS")
        self.assertEqual(parsed.language, "c")
        self.assertEqual(parsed.spans[0].start_byte, 0)
        self.assertEqual(parsed.spans[-1].end_byte, len(raw))
        self.assertEqual(
            b"".join(raw[span.start_byte : span.end_byte] for span in parsed.spans), raw
        )
        kinds = {kind for span in parsed.spans for kind in span.structural_kinds}
        self.assertIn("MACRO", kinds)
        self.assertIn("TYPE", kinds)
        self.assertIn("FUNCTION", kinds)

    def test_cpp_header_uses_cpp_grammar_when_it_scores_better(self) -> None:
        raw = b"namespace demo { class Box { public: int value() const; }; }\n"
        parsed = self.parser.parse(raw, extension=".h", source_encoding="utf-8")
        self.assertEqual(parsed.outcome, "PARSE_SUCCESS")
        self.assertEqual(parsed.language, "cpp")
        self.assertEqual(parsed.spans[-1].end_byte, len(raw))

    def test_malformed_source_falls_back_without_losing_bytes(self) -> None:
        raw = b"int broken( {\nreturn 1;\n"
        parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
        self.assertEqual(parsed.outcome, "FALLBACK_SUCCESS")
        self.assertEqual(parsed.fallback_reason, "syntax_error_or_missing_node")
        self.assertEqual(
            b"".join(raw[span.start_byte : span.end_byte] for span in parsed.spans), raw
        )

    def test_cp1252_source_is_explicit_fallback(self) -> None:
        raw = b"// caf\xe9\nint value;\n"
        text, encoding = decode_source(raw)
        self.assertEqual(text, "// café\nint value;\n")
        self.assertEqual(encoding, "cp1252")
        parsed = self.parser.parse(raw, extension=".c", source_encoding=encoding)
        self.assertEqual(parsed.outcome, "FALLBACK_SUCCESS")
        self.assertEqual(parsed.fallback_reason, "non_utf8_source")

    def test_invalid_nul_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "NUL"):
            decode_source(b"int x;\x00")

    def test_utf8_and_crlf_windowing_preserves_raw_bytes(self) -> None:
        raw = (("가" * 19) + "\r\n" + ("나" * 19) + "\n").encode("utf-8")
        windows = deterministic_windows(
            raw, encoding="utf-8", max_rendered_utf8_bytes=31
        )
        self.assertGreater(len(windows), 2)
        self.assertEqual(b"".join(raw[start:end] for start, end in windows), raw)
        for start, end in windows:
            raw[start:end].decode("utf-8", errors="strict")
            self.assertLessEqual(len(raw[start:end]), 31)

    def test_oversized_function_splits_on_ast_or_line_boundaries(self) -> None:
        statements = b"".join(f"x += {index};\n".encode("ascii") for index in range(900))
        raw = b"int f(void) { int x = 0;\n" + statements + b"return x; }\n"
        parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
        self.assertEqual(parsed.outcome, "PARSE_SUCCESS")
        self.assertGreater(len(parsed.spans), 1)
        self.assertTrue(all(span.end_byte - span.start_byte <= 3000 for span in parsed.spans))
        self.assertTrue(all(span.kind == "AST_BLOCK" for span in parsed.spans))
        self.assertTrue(all(span.parent_kind == "FUNCTION" for span in parsed.spans))
        self.assertEqual(parsed.spans[-1].end_byte, len(raw))

    def test_adjacent_functions_remain_separate_structural_units(self) -> None:
        raw = b"int first(void) { return 1; }\nint second(void) { return 2; }\n"
        parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
        functions = [span for span in parsed.spans if span.kind == "FUNCTION"]
        self.assertEqual(len(functions), 2)
        self.assertNotEqual(
            functions[0].structural_unit_start_byte,
            functions[1].structural_unit_start_byte,
        )

    def test_header_guard_exposes_macro_type_and_function_units(self) -> None:
        raw = (
            b"#ifndef FIXTURE_H\n#define FIXTURE_H\n"
            b"typedef struct Item { int value; } Item;\n"
            b"int get_value(Item *item) { return item->value; }\n"
            b"#endif\n"
        )
        parsed = self.parser.parse(raw, extension=".h", source_encoding="utf-8")
        kinds = {span.kind for span in parsed.spans}
        self.assertIn("MACRO", kinds)
        self.assertIn("TYPE", kinds)
        self.assertIn("FUNCTION", kinds)

    def test_chunk_identity_is_content_addressed_and_stable(self) -> None:
        raw = b"int f(void) { return 7; }\n"
        parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
        source = source_record("fixture/test.c", raw)
        kwargs = {
            "source_record": source,
            "raw": raw,
            "parsed": parsed,
            "target_commit_sha": "a" * 40,
            "target_scope_sha256": "b" * 64,
        }
        first = build_file_chunks(**kwargs)
        second = build_file_chunks(**kwargs)
        self.assertEqual(first, second)
        self.assertRegex(first[0]["chunk_id"], r"^C1-[0-9a-f]{24}$")
        self.assertEqual(first[0]["token_count"], first[0]["evidence_frame_utf8_bytes"])
        self.assertLessEqual(first[0]["token_count"], 8192)

    def test_chunk_end_line_is_inclusive(self) -> None:
        raw = b"int value;\n"
        parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
        chunks = build_file_chunks(
            source_record=source_record("fixture/end_line.c", raw),
            raw=raw,
            parsed=parsed,
            target_commit_sha="a" * 40,
            target_scope_sha256="b" * 64,
        )
        self.assertEqual(chunks[0]["end_line"], 1)
        self.assertEqual(chunks[0]["end_point_line"], 2)
        self.assertEqual(chunks[0]["end_column_byte_exclusive"], 0)

    def test_next_fit_batch_budget_boundary(self) -> None:
        raw_a = b"int a;\n"
        raw_b = b"int b;\n"
        chunks: list[dict[str, Any]] = []
        raw_by_path: dict[str, bytes] = {}
        for path, raw in (("a.c", raw_a), ("b.c", raw_b)):
            parsed = self.parser.parse(raw, extension=".c", source_encoding="utf-8")
            chunks.extend(
                build_file_chunks(
                    source_record=source_record(path, raw),
                    raw=raw,
                    parsed=parsed,
                    target_commit_sha="a" * 40,
                    target_scope_sha256="b" * 64,
                )
            )
            raw_by_path[path] = raw

        def loader(chunk: dict[str, Any]) -> str:
            raw = raw_by_path[chunk["path"]][chunk["start_byte"] : chunk["end_byte_exclusive"]]
            return rendered_slice(raw, chunk["source_encoding"])

        exact = sum(chunk["evidence_frame_utf8_bytes"] for chunk in chunks)
        common = {
            "chunks": chunks,
            "content_loader": loader,
            "target_commit_sha": "a" * 40,
            "target_scope_sha256": "b" * 64,
            "source_manifest_sha256": "c" * 64,
            "chunking_contract_sha256": "d" * 64,
            "file_manifest_sha256": "e" * 64,
            "chunk_manifest_sha256": "f" * 64,
        }
        self.assertEqual(len(build_batches(**common, max_payload_utf8_bytes=exact)), 1)
        self.assertEqual(len(build_batches(**common, max_payload_utf8_bytes=exact - 1)), 2)


class FullArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = PROJECT_ROOT / ".target-src" / "userland"
        cls.git = VERIFY.find_git(None)

    def verify(self, **overrides: Any) -> dict[str, Any]:
        parameters = {
            "root": PROJECT_ROOT,
            "repo": self.repo,
            "git_executable": self.git,
            "check_lock": True,
            "rebuild_check": False,
        }
        parameters.update(overrides)
        return VERIFY.verify(**parameters)

    def test_full_userland_contract(self) -> None:
        result = self.verify()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["files"], 654)
        self.assertEqual(result["parse_success"] + result["fallback_success"], 654)
        self.assertEqual(len(result["selected_batch_ids"]), 3)

    def test_worktree_poison_does_not_change_git_blob_reader(self) -> None:
        scratch = PROJECT_ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phase2-blob-fixture-", dir=scratch) as temp:
            fixture = Path(temp)
            subprocess.run([str(self.git), "init", "--quiet", str(fixture)], check=True)
            original = b"int locked_value = 1;\n"
            path = fixture / "fixture.c"
            path.write_bytes(original)
            result = subprocess.run(
                [str(self.git), "-C", str(fixture), "hash-object", "-w", "fixture.c"],
                check=True,
                capture_output=True,
                text=True,
            )
            oid = result.stdout.strip()
            path.write_bytes(b"int poisoned_worktree_value = 999;\r\n")
            with GitBlobSource(git_executable=self.git, repository=fixture) as source:
                self.assertEqual(source.read_blob(oid, expected_size=len(original)), original)

    def test_chunk_hash_tamper_fails_closed(self) -> None:
        scratch = PROJECT_ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phase2-chunk-tamper-", dir=scratch) as temp:
            copied = Path(temp) / "chunking"
            shutil.copytree(PROJECT_ROOT / "artifacts" / "chunking", copied)
            records, _ = VERIFY.load_jsonl(copied / "chunk_manifest.jsonl")
            records[0]["raw_content_sha256"] = "0" * 64
            write_jsonl(copied / "chunk_manifest.jsonl", records)
            with self.assertRaises(VERIFY.VerificationError):
                self.verify(chunking_dir=copied, check_lock=False)

    def test_batch_membership_tamper_fails_closed(self) -> None:
        scratch = PROJECT_ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phase2-batch-tamper-", dir=scratch) as temp:
            copied = Path(temp) / "chunking"
            shutil.copytree(PROJECT_ROOT / "artifacts" / "chunking", copied)
            records, _ = VERIFY.load_jsonl(copied / "batch_manifest.jsonl")
            records[0]["chunk_ids"] = records[0]["chunk_ids"] + [records[0]["chunk_ids"][0]]
            records[0]["chunk_count"] += 1
            write_jsonl(copied / "batch_manifest.jsonl", records)
            with self.assertRaises(VERIFY.VerificationError):
                self.verify(chunking_dir=copied, check_lock=False)

    def test_selection_rank_tamper_fails_closed(self) -> None:
        scratch = PROJECT_ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phase2-selection-tamper-", dir=scratch) as temp:
            selection_path = Path(temp) / "selection.json"
            selection = VERIFY.load_json(
                PROJECT_ROOT / "artifacts" / "evaluation" / "selection.json"
            )
            selection["selected_batch_ids"] = list(reversed(selection["selected_batch_ids"]))
            write_json(selection_path, selection)
            with self.assertRaises(VERIFY.VerificationError):
                self.verify(selection_path=selection_path, check_lock=False)

    def test_unsupported_schema_keyword_fails_closed(self) -> None:
        with self.assertRaises(VERIFY.VerificationError):
            VERIFY.validate_schema({}, {"type": "object", "contains": {}})

    def test_schema_const_does_not_treat_boolean_as_integer(self) -> None:
        with self.assertRaises(VERIFY.VerificationError):
            VERIFY.validate_schema(True, {"type": "integer", "const": 1})

    def test_ast_metadata_tamper_fails_closed(self) -> None:
        invalid = {
            "chunk_id": "C1-" + "0" * 24,
            "kind": "FUNCTION",
            "node_type": "if_statement",
            "parse_mode": "PARSE_SUCCESS",
            "structural_kinds": ["FUNCTION"],
        }
        with self.assertRaises(VERIFY.VerificationError):
            VERIFY.validate_chunk_semantics(invalid)

    def test_experiment_cap_drift_fails_before_build(self) -> None:
        lock = copy.deepcopy(BUILD.load_json(PROJECT_ROOT / "experiment.lock.yaml"))
        lock["semantic"]["context_envelope"]["component_caps"][
            "evidence_or_raw_batch_tokens"
        ] = 2000
        with self.assertRaises(BUILD.Phase2Error):
            BUILD.validate_locked_batch_budget(lock)

    def test_frozen_binding_mismatch_fails_before_any_write(self) -> None:
        batch_path = PROJECT_ROOT / "artifacts" / "chunking" / "batch_manifest.jsonl"
        selection_path = PROJECT_ROOT / "artifacts" / "evaluation" / "selection.json"
        before = (batch_path.read_bytes(), selection_path.read_bytes())
        with self.assertRaises(BUILD.Phase2Error):
            BUILD.prepare_experiment_freeze(
                root=PROJECT_ROOT,
                batch_manifest_bytes=before[0] + b"tamper",
                selection_bytes=before[1],
                allow_pre_result_refreeze=False,
            )
        self.assertEqual(batch_path.read_bytes(), before[0])
        self.assertEqual(selection_path.read_bytes(), before[1])

    def test_frozen_lock_identity_changed_from_phase1(self) -> None:
        lock = VERIFY.load_json(PROJECT_ROOT / "experiment.lock.yaml")
        self.assertEqual(lock["semantic"]["lock_stage"], "evaluation_frozen")
        self.assertNotEqual(lock["identity"]["experiment_id"], "exp-v1-66575973a148987d741ba3f9")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--verbose", action="store_true")
    arguments, unittest_arguments = parser.parse_known_args()
    argv = [sys.argv[0], *unittest_arguments]
    program = unittest.main(
        argv=argv,
        verbosity=2 if arguments.verbose else 1,
        exit=False,
    )
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
