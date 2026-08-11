#!/usr/bin/env python3
"""Positive and negative tests for the Phase 1 experiment contract."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock
from urllib import error as urlerror
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
VERIFIER_PATH = PROJECT_ROOT / "scripts" / "verify_experiment_lock.py"
SPEC = importlib.util.spec_from_file_location("phase1_verifier", VERIFIER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load the independent Phase 1 verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)

from src.providers import OllamaProvider, ProviderError  # noqa: E402


class FakeHttpResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


def reseal(lock: dict[str, Any]) -> None:
    digest, experiment_id = VERIFIER.semantic_identity(lock["semantic"])
    lock["identity"]["semantic_sha256"] = digest
    lock["identity"]["experiment_id"] = experiment_id


class Phase1ContractTests(unittest.TestCase):
    live = False

    @classmethod
    def setUpClass(cls) -> None:
        cls.lock = VERIFIER.load_json(PROJECT_ROOT / "experiment.lock.yaml")
        cls.target_lock = VERIFIER.load_json(PROJECT_ROOT / "target.lock.yaml")
        cls.artifact_paths = [
            PROJECT_ROOT / entry["path"] for entry in cls.lock["evidence"]["artifacts"]
        ] + [
            PROJECT_ROOT / "experiment.lock.yaml",
            PROJECT_ROOT / "schemas" / "experiment-lock.schema.json",
        ]

    def test_complete_contract_verifies_without_mutation(self) -> None:
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.artifact_paths
        }
        verified = VERIFIER.verify(PROJECT_ROOT, live=self.live)
        self.assertEqual(verified["identity"], self.lock["identity"])
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.artifact_paths
        }
        self.assertEqual(before, after)

    def test_semantic_change_requires_new_experiment_id(self) -> None:
        changed = copy.deepcopy(self.lock)
        old_id = changed["identity"]["experiment_id"]
        changed["semantic"]["runtime_profile"]["options"]["temperature"] = 0.01
        new_digest, new_id = VERIFIER.semantic_identity(changed["semantic"])
        self.assertNotEqual(new_id, old_id)
        self.assertNotEqual(new_digest, changed["identity"]["semantic_sha256"])
        with self.assertRaises(VERIFIER.VerificationError):
            VERIFIER.validate_semantics(changed, self.target_lock)

    def test_volatile_evidence_change_preserves_experiment_id(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["evidence"]["captured_at_utc"] = "2030-01-01T00:00:00Z"
        digest, experiment_id = VERIFIER.semantic_identity(changed["semantic"])
        self.assertEqual(digest, self.lock["identity"]["semantic_sha256"])
        self.assertEqual(experiment_id, self.lock["identity"]["experiment_id"])

    def test_context_overflow_fails_closed(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["semantic"]["context_envelope"]["component_caps"][
            "evidence_or_raw_batch_tokens"
        ] += 1
        reseal(changed)
        with self.assertRaises(VERIFIER.VerificationError):
            VERIFIER.validate_semantics(changed, self.target_lock)

    def test_reserved_output_must_equal_num_predict(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["semantic"]["runtime_profile"]["options"]["num_predict"] -= 1
        reseal(changed)
        with self.assertRaises(VERIFIER.VerificationError):
            VERIFIER.validate_semantics(changed, self.target_lock)

    def test_phase1_rejects_fabricated_selection_binding(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["semantic"]["evaluation_binding"] = {
            "batch_manifest_sha256": "0" * 64,
            "selection_sha256": "1" * 64,
        }
        reseal(changed)
        with self.assertRaises(VERIFIER.VerificationError):
            VERIFIER.validate_semantics(changed, self.target_lock)

    def test_frozen_stage_requires_selection_digests(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["semantic"]["lock_stage"] = "evaluation_frozen"
        reseal(changed)
        with self.assertRaises(VERIFIER.VerificationError):
            VERIFIER.validate_semantics(changed, self.target_lock)

    def test_model_digest_urn_cannot_drift(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["semantic"]["model"]["manifest_digest_urn"] = "sha256:" + "0" * 64
        reseal(changed)
        with self.assertRaises(VERIFIER.VerificationError):
            VERIFIER.validate_semantics(changed, self.target_lock)

    def test_provider_rejects_invalid_digest(self) -> None:
        with self.assertRaises(ValueError):
            OllamaProvider(
                base_url="http://127.0.0.1:11434",
                model="fixture-model",
                request_timeout_seconds=1,
                max_retries=0,
                retry_backoff_seconds=[],
                expected_model_digest="not-a-full-digest",
            )

    def test_provider_fails_closed_on_tag_digest_drift(self) -> None:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="fixture-model",
            request_timeout_seconds=1,
            max_retries=0,
            retry_backoff_seconds=[],
            expected_model_digest="a" * 64,
        )
        provider.tags = lambda **_: {
            "models": [{"name": "fixture-model", "digest": "b" * 64}]
        }
        with self.assertRaises(ProviderError):
            provider.assert_model_identity()

    def locked_provider(self) -> OllamaProvider:
        provider = OllamaProvider.from_experiment_lock(self.lock)
        provider.assert_model_identity = lambda: None
        return provider

    def valid_locked_request(self) -> dict[str, Any]:
        components = {
            "system_instruction": "system",
            "user_instruction": "instruction",
            "evidence_or_raw_batch": "evidence",
            "structured_state": "state",
        }
        return {
            "messages": [
                {"role": "system", "content": components["system_instruction"]},
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            components["user_instruction"],
                            components["evidence_or_raw_batch"],
                            components["structured_state"],
                        ]
                    ),
                },
            ],
            "response_schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "options": self.lock["semantic"]["runtime_profile"]["options"],
            "stream": False,
            "keep_alive": self.lock["semantic"]["runtime_profile"]["transport"]["keep_alive"],
            "tools": None,
            "component_payloads": components,
        }

    def test_locked_provider_rejects_option_drift_before_inference(self) -> None:
        provider = self.locked_provider()
        request_value = self.valid_locked_request()
        request_value["options"] = dict(request_value["options"])
        request_value["options"]["temperature"] = 0.5
        provider._request_json = mock.Mock()
        with self.assertRaises(ProviderError):
            provider.chat(**request_value)
        provider._request_json.assert_not_called()

    def test_locked_provider_rejects_component_overflow_before_inference(self) -> None:
        provider = self.locked_provider()
        request_value = self.valid_locked_request()
        cap = self.lock["semantic"]["context_envelope"]["component_caps"][
            "system_instruction_tokens"
        ]
        request_value["component_payloads"] = dict(request_value["component_payloads"])
        request_value["component_payloads"]["system_instruction"] = "s" * (cap + 1)
        request_value["messages"] = [dict(item) for item in request_value["messages"]]
        request_value["messages"][0]["content"] = request_value["component_payloads"][
            "system_instruction"
        ]
        provider._request_json = mock.Mock()
        with self.assertRaises(ProviderError):
            provider.chat(**request_value)
        provider._request_json.assert_not_called()

    def test_locked_provider_accepts_bound_structured_request(self) -> None:
        provider = self.locked_provider()
        provider._request_json = mock.Mock(
            return_value={
                "model": self.lock["semantic"]["model"]["name"],
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 20,
                "eval_count": 5,
                "message": {"role": "assistant", "content": "{\"ok\":true}"},
            }
        )
        response = provider.chat(**self.valid_locked_request())
        self.assertTrue(response["done"])
        provider._request_json.assert_called_once()

    def test_locked_provider_rejects_duplicate_structured_key(self) -> None:
        provider = self.locked_provider()
        with self.assertRaises(ProviderError):
            provider._validate_locked_response(
                {
                    "model": self.lock["semantic"]["model"]["name"],
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 20,
                    "eval_count": 5,
                    "message": {
                        "role": "assistant",
                        "content": "{\"ok\":false,\"ok\":true}",
                    },
                }
            )

    def test_locked_provider_rejects_response_schema_violation(self) -> None:
        provider = self.locked_provider()
        with self.assertRaises(ProviderError):
            provider._validate_locked_response(
                {
                    "model": self.lock["semantic"]["model"]["name"],
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 20,
                    "eval_count": 5,
                    "message": {
                        "role": "assistant",
                        "content": "{\"ok\":true,\"extra\":1}",
                    },
                },
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            )

    def test_locked_provider_enforces_array_min_items(self) -> None:
        provider = self.locked_provider()
        with self.assertRaises(ProviderError):
            provider._validate_locked_response(
                {
                    "model": self.lock["semantic"]["model"]["name"],
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 20,
                    "eval_count": 2,
                    "message": {"role": "assistant", "content": "[]"},
                },
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            )

    def test_locked_provider_rejects_empty_or_unsupported_schema_before_inference(self) -> None:
        provider = self.locked_provider()
        for invalid_schema in ({}, {"type": "string", "format": "email"}):
            request_value = self.valid_locked_request()
            request_value["response_schema"] = invalid_schema
            provider._request_json = mock.Mock()
            with self.assertRaises(ProviderError):
                provider.chat(**request_value)
            provider._request_json.assert_not_called()

    def test_locked_provider_requires_assistant_stop_envelope(self) -> None:
        provider = self.locked_provider()
        base_response = {
            "model": self.lock["semantic"]["model"]["name"],
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 20,
            "eval_count": 5,
            "message": {"role": "assistant", "content": "{\"ok\":true}"},
        }
        wrong_role = copy.deepcopy(base_response)
        wrong_role["message"]["role"] = "tool"
        with self.assertRaises(ProviderError):
            provider._validate_locked_response(wrong_role)
        wrong_reason = copy.deepcopy(base_response)
        wrong_reason["done_reason"] = "other"
        with self.assertRaises(ProviderError):
            provider._validate_locked_response(wrong_reason)

    def test_provider_factory_rejects_unsealed_semantic_drift(self) -> None:
        changed = copy.deepcopy(self.lock)
        changed["semantic"]["runtime_profile"]["options"]["temperature"] = 0.5
        with self.assertRaises(ProviderError):
            OllamaProvider.from_experiment_lock(changed)

    def test_provider_retries_500_then_succeeds(self) -> None:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="fixture-model",
            request_timeout_seconds=1,
            max_retries=2,
            retry_backoff_seconds=[0, 0],
        )
        server_error = urlerror.HTTPError(
            "http://127.0.0.1/api/tags", 500, "server", {}, io.BytesIO(b"server")
        )
        with mock.patch(
            "src.providers.ollama.request.urlopen",
            side_effect=[server_error, FakeHttpResponse(b'{"models":[]}')],
        ) as opened:
            self.assertEqual(provider.tags(), {"models": []})
        self.assertEqual(opened.call_count, 2)

    def test_provider_does_not_retry_non_429_4xx(self) -> None:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="fixture-model",
            request_timeout_seconds=1,
            max_retries=2,
            retry_backoff_seconds=[0, 0],
        )
        bad_request = urlerror.HTTPError(
            "http://127.0.0.1/api/tags", 400, "bad", {}, io.BytesIO(b"bad")
        )
        with mock.patch(
            "src.providers.ollama.request.urlopen", side_effect=bad_request
        ) as opened:
            with self.assertRaises(ProviderError):
                provider.tags()
        self.assertEqual(opened.call_count, 1)

    def test_provider_retries_timeout_exactly(self) -> None:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="fixture-model",
            request_timeout_seconds=0.01,
            max_retries=2,
            retry_backoff_seconds=[0, 0],
        )
        with mock.patch(
            "src.providers.ollama.request.urlopen", side_effect=socket.timeout("timeout")
        ) as opened:
            with self.assertRaises(ProviderError):
                provider.tags()
        self.assertEqual(opened.call_count, 3)

    def test_provider_rejects_remote_endpoint_by_default(self) -> None:
        with self.assertRaises(ValueError):
            OllamaProvider(
                base_url="https://llm.example.invalid",
                model="fixture-model",
                request_timeout_seconds=1,
                max_retries=0,
                retry_backoff_seconds=[],
            )

    def new_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="ai-sast-phase1-")
        root = Path(temporary.name)
        (root / "schemas").mkdir()
        (root / "artifacts").mkdir()
        shutil.copy2(PROJECT_ROOT / "target.lock.yaml", root / "target.lock.yaml")
        shutil.copy2(PROJECT_ROOT / "experiment.lock.yaml", root / "experiment.lock.yaml")
        shutil.copy2(
            PROJECT_ROOT / "schemas" / "experiment-lock.schema.json",
            root / "schemas" / "experiment-lock.schema.json",
        )
        shutil.copytree(
            PROJECT_ROOT / "artifacts" / "preflight",
            root / "artifacts" / "preflight",
        )
        return temporary, root

    @staticmethod
    def write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def reseal_evidence_artifact(self, root: Path, artifact_name: str) -> None:
        lock_path = root / "experiment.lock.yaml"
        lock = VERIFIER.load_json(lock_path)
        entry = next(
            item for item in lock["evidence"]["artifacts"] if item["name"] == artifact_name
        )
        artifact = root / entry["path"]
        raw = artifact.read_bytes()
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
        entry["size_bytes"] = len(raw)
        self.write_json(lock_path, lock)

    def test_verifier_rejects_stored_smoke_content_drift(self) -> None:
        temporary, root = self.new_fixture()
        try:
            path = root / "artifacts" / "preflight" / "structured_output_smoke.json"
            smoke = VERIFIER.load_json(path)
            smoke["responses"][0]["message"]["content"] = (
                '{"status":"BAD","status":"OK","backend":"ollama","seed_echo":20260811}'
            )
            self.write_json(path, smoke)
            self.reseal_evidence_artifact(root, "structured_output_smoke")
            with self.assertRaises(VERIFIER.VerificationError):
                VERIFIER.verify(root, live=False)
        finally:
            temporary.cleanup()

    def test_verifier_rejects_context_counter_claim_drift(self) -> None:
        temporary, root = self.new_fixture()
        try:
            path = root / "artifacts" / "preflight" / "context_envelope.json"
            envelope = VERIFIER.load_json(path)
            envelope["probe"]["observations"][-1]["component_delta_tokens"] += 1
            envelope["probe"]["component_counts"][
                "tool_and_response_schema_tokens"
            ] += 1
            envelope["probe"]["component_count_sum"] += 1
            self.write_json(path, envelope)
            self.reseal_evidence_artifact(root, "context_envelope")
            with self.assertRaises(VERIFIER.VerificationError):
                VERIFIER.verify(root, live=False)
        finally:
            temporary.cleanup()

    def test_phase2_frozen_stage_preserves_historical_preflight_receipt(self) -> None:
        temporary, root = self.new_fixture()
        try:
            lock_path = root / "experiment.lock.yaml"
            lock = VERIFIER.load_json(lock_path)
            contract = lock["semantic"]["evaluation_contract"]
            batch_ids = ["BATCH-001", "BATCH-002", "BATCH-003", "BATCH-004", "BATCH-005"]
            batch_path = root / contract["batch_manifest_artifact"]
            batch_path.parent.mkdir(parents=True, exist_ok=True)
            batch_path.write_text(
                "".join(json.dumps({"batch_id": value}) + "\n" for value in batch_ids),
                encoding="utf-8",
                newline="\n",
            )
            batch_sha = hashlib.sha256(batch_path.read_bytes()).hexdigest()
            domain = contract["selection_hash_domain"].encode("utf-8")
            seed = str(contract["selection_seed"]).encode("ascii")
            ranked = sorted(
                batch_ids,
                key=lambda batch_id: (
                    hashlib.sha256(
                        domain + b"\0" + seed + b"\0" + batch_id.encode("utf-8")
                    ).hexdigest(),
                    batch_id,
                ),
            )
            selection = {
                "schema_version": 1,
                "target_commit_sha": lock["semantic"]["target_binding"]["target_commit_sha"],
                "target_scope_sha256": lock["semantic"]["target_binding"]["target_scope_sha256"],
                "batch_manifest_path": contract["batch_manifest_artifact"],
                "batch_manifest_sha256": batch_sha,
                "selection_seed": contract["selection_seed"],
                "selection_seed_encoding": contract["selection_seed_encoding"],
                "selection_algorithm": contract["selection_algorithm"],
                "selection_hash_domain": contract["selection_hash_domain"],
                "batch_count": contract["batch_count"],
                "selected_batch_ids": ranked[: contract["batch_count"]],
                "frozen_before_any_llm_result": True,
                "replace_after_result_observation": False,
            }
            selection_path = root / contract["shared_selection_artifact"]
            self.write_json(selection_path, selection)
            lock["semantic"]["lock_stage"] = "evaluation_frozen"
            lock["semantic"]["evaluation_binding"] = {
                "batch_manifest_path": contract["batch_manifest_artifact"],
                "batch_manifest_sha256": batch_sha,
                "selection_path": contract["shared_selection_artifact"],
                "selection_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
            }
            reseal(lock)
            self.write_json(lock_path, lock)
            verified = VERIFIER.verify(root, live=False)
            self.assertEqual(verified["semantic"]["lock_stage"], "evaluation_frozen")
        finally:
            temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--live", action="store_true")
    known, remaining = parser.parse_known_args()
    Phase1ContractTests.live = known.live
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
