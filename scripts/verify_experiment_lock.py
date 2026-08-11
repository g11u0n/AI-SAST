#!/usr/bin/env python3
"""Independent verifier for the Phase 1 experiment lock and evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HASH_DOMAIN = b"ai-sast-experiment-semantic-v1\0"
EXPERIMENT_ID_PREFIX = "exp-v1-"
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|TBC|FIXME|CHANGEME|REPLACE_ME|PLACEHOLDER|ESTIMATE|UNKNOWN)\b"
    r"|\$\{|\{\{|<[^>]+>|example\.com",
    re.IGNORECASE,
)
SMOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["OK"]},
        "backend": {"type": "string", "enum": ["ollama"]},
        "seed_echo": {"type": "integer", "enum": [20260811]},
    },
    "required": ["status", "backend", "seed_echo"],
    "additionalProperties": False,
}
SMOKE_MESSAGES = [
    {"role": "system", "content": "Return only JSON matching the provided schema."},
    {
        "role": "user",
        "content": (
            "Preflight check. Set status to OK, backend to ollama, "
            "and seed_echo to 20260811."
        ),
    },
]
EXPECTED_SMOKE = {"status": "OK", "backend": "ollama", "seed_echo": 20260811}
ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "enum": [True]}},
    "required": ["ok"],
    "additionalProperties": False,
}
EXPECTED_ENVELOPE = {"ok": True}
CONTEXT_RECIPE = {
    "system_marker": "s",
    "system_repetitions": 720,
    "user_instruction_marker": "u",
    "user_instruction_repetitions": 200,
    "evidence_marker": "e",
    "evidence_repetitions": 3700,
    "structured_state_marker": "q",
    "structured_state_repetitions": 220,
    "tool_description_marker": "t",
    "tool_description_repetitions": 350,
}


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


def read_utf8_no_bom_lf(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail(f"UTF-8 BOM is not allowed: {path}")
    if b"\r" in raw:
        fail(f"CR/CRLF is not allowed: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"Invalid UTF-8 in {path}: {exc}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(read_utf8_no_bom_lf(path), object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON in {path}: {exc}")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    text = read_utf8_no_bom_lf(path)
    if not text.endswith("\n"):
        fail(f"JSONL must end with LF: {path}")
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            fail(f"Blank JSONL record at {path}:{line_number}")
        try:
            value = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            fail(f"Invalid JSONL at {path}:{line_number}: {exc}")
        if not isinstance(value, dict):
            fail(f"JSONL record must be an object at {path}:{line_number}")
        result.append(value)
    return result


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        fail(f"Semantic value is not canonical JSON: {exc}")


def semantic_identity(semantic: dict[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(HASH_DOMAIN + canonical_json(semantic)).hexdigest()
    return digest, EXPERIMENT_ID_PREFIX + digest[:24]


def preflight_stage_identity(semantic: dict[str, Any]) -> tuple[str, str]:
    """Reconstruct the immutable Phase 1 receipt identity from a later stage."""
    preflight_semantic = copy.deepcopy(semantic)
    preflight_semantic["lock_stage"] = "preflight_locked"
    preflight_semantic.pop("evaluation_binding", None)
    return semantic_identity(preflight_semantic)


def assert_no_placeholders(value: Any, location: str = "$") -> None:
    if isinstance(value, str):
        if not value.strip():
            fail(f"Empty string at {location}")
        match = PLACEHOLDER_RE.search(value)
        if match:
            fail(f"Placeholder-like value at {location}: {match.group(0)}")
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_no_placeholders(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_placeholders(item, f"{location}[{index}]")


def type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_schema(instance: Any, schema: dict[str, Any], location: str = "$") -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(type_matches(instance, item) for item in expected):
            fail(f"Schema type violation at {location}: expected one of {expected}")
    elif isinstance(expected, str) and not type_matches(instance, expected):
        fail(f"Schema type violation at {location}: expected {expected}")
    if "const" in schema and instance != schema["const"]:
        fail(f"Schema const violation at {location}")
    if "enum" in schema and instance not in schema["enum"]:
        fail(f"Schema enum violation at {location}")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in instance]
        if missing:
            fail(f"Schema required-property violation at {location}: {missing}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                fail(f"Schema additional-property violation at {location}: {extras}")
        for key, value in instance.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                validate_schema(value, child_schema, f"{location}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(value, schema["additionalProperties"], f"{location}.{key}")

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            fail(f"Schema minItems violation at {location}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            fail(f"Schema maxItems violation at {location}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(instance):
                validate_schema(value, item_schema, f"{location}[{index}]")

    if isinstance(instance, str):
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            fail(f"Schema pattern violation at {location}")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError:
                fail(f"Schema date-time violation at {location}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            fail(f"Schema minimum violation at {location}")
        if "maximum" in schema and instance > schema["maximum"]:
            fail(f"Schema maximum violation at {location}")


def strict_expected_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        fail(f"Unexpected keys at {location}: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")


def validate_semantics(lock: dict[str, Any], target_lock: dict[str, Any]) -> None:
    strict_expected_keys(lock, {"schema_version", "identity", "semantic", "evidence", "invalidation"}, "$")
    if lock["schema_version"] != 1:
        fail("Unsupported experiment lock schema version")
    semantic = lock["semantic"]
    digest, experiment_id = semantic_identity(semantic)
    if lock["identity"]["semantic_sha256"] != digest:
        fail("Experiment semantic SHA-256 mismatch")
    if lock["identity"]["experiment_id"] != experiment_id:
        fail("Experiment ID mismatch")
    if lock["identity"]["hash_domain"] != "ai-sast-experiment-semantic-v1":
        fail("Experiment hash domain mismatch")

    target = semantic["target_binding"]
    if target["target_commit_sha"] != target_lock["target"]["commit_sha"]:
        fail("Experiment target commit is not bound to target.lock.yaml")
    if target["target_scope_sha256"] != target_lock["analysis_scope"]["scope_sha256"]:
        fail("Experiment target scope is not bound to target.lock.yaml")
    if target["target_file_list_sha256"] != target_lock["analysis_scope"]["target_file_list_sha256"]:
        fail("Experiment file-list hash is not bound to target.lock.yaml")

    backend = semantic["backend"]
    strict_expected_keys(
        backend,
        {"provider", "base_url", "ollama_version", "api_routes"},
        "$.semantic.backend",
    )
    if backend["provider"] != "ollama":
        fail("v1 runtime provider must be Ollama")
    if re.fullmatch(r"http://(?:127\.0\.0\.1|localhost)(?::[0-9]+)?", backend["base_url"], re.IGNORECASE) is None:
        fail("v1 Ollama endpoint must be loopback HTTP")
    expected_routes = {
        "version": "/api/version",
        "tags": "/api/tags",
        "show": "/api/show",
        "chat": "/api/chat",
        "running_models": "/api/ps",
    }
    if backend["api_routes"] != expected_routes:
        fail("Ollama API route contract drifted")
    model = semantic["model"]
    digest_value = model["manifest_digest"]
    if not isinstance(digest_value, str) or DIGEST_RE.fullmatch(digest_value) is None:
        fail("Model manifest digest is not a full 64-hex SHA-256")
    if model["manifest_digest_urn"] != "sha256:" + digest_value:
        fail("Model digest URN mismatch")
    if model["manifest_digest_algorithm"] != "sha256":
        fail("Model digest algorithm mismatch")
    if model["native_context_length"] < semantic["runtime_profile"]["options"]["num_ctx"]:
        fail("Runtime num_ctx exceeds the model context length")

    profile = semantic["runtime_profile"]
    strict_expected_keys(
        profile,
        {"transport", "options", "structured_output", "retry", "timeouts_seconds"},
        "$.semantic.runtime_profile",
    )
    options = profile["options"]
    transport = profile["transport"]
    strict_expected_keys(
        transport,
        {
            "stream", "keep_alive", "stateless_per_call", "max_concurrency",
            "loopback_only", "max_response_bytes",
        },
        "$.semantic.runtime_profile.transport",
    )
    if profile["transport"]["stream"] is not False:
        fail("v1 structured calls must use stream=false")
    if profile["transport"]["stateless_per_call"] is not True:
        fail("v1 calls must be stateless")
    if profile["transport"]["max_concurrency"] != 1:
        fail("Phase 1 locks max_concurrency to one")
    if transport["loopback_only"] is not True:
        fail("v1 source code must stay on a loopback Ollama endpoint")
    if not isinstance(transport["max_response_bytes"], int) or isinstance(transport["max_response_bytes"], bool) or transport["max_response_bytes"] <= 0:
        fail("Provider response byte limit must be a positive integer")
    if not isinstance(transport["keep_alive"], str) or re.fullmatch(r"[1-9][0-9]*[smh]", transport["keep_alive"]) is None:
        fail("Ollama keep_alive must be an explicit positive duration")
    expected_option_keys = {
        "num_ctx", "num_predict", "temperature", "seed", "top_k", "top_p",
        "min_p", "repeat_last_n", "repeat_penalty", "stop",
    }
    strict_expected_keys(options, expected_option_keys, "$.semantic.runtime_profile.options")
    integer_options = ("num_ctx", "num_predict", "seed", "top_k", "repeat_last_n")
    if any(not isinstance(options[key], int) or isinstance(options[key], bool) or options[key] < 0 for key in integer_options):
        fail("Integer generation options must be non-negative integers")
    if options["num_ctx"] <= 0 or options["num_predict"] <= 0:
        fail("num_ctx and num_predict must be positive")
    for key in ("temperature", "top_p", "min_p", "repeat_penalty"):
        if not isinstance(options[key], (int, float)) or isinstance(options[key], bool) or options[key] < 0:
            fail(f"Generation option must be a non-negative number: {key}")
    if not 0 <= options["top_p"] <= 1 or not 0 <= options["min_p"] <= 1:
        fail("top_p and min_p must be within [0, 1]")
    if options["stop"] != []:
        fail("Phase 1 locks an empty explicit stop list")
    if profile["structured_output"] != {
        "method": "ollama_chat_format_json_schema",
        "local_strict_json_validation": True,
        "additional_properties_default": "reject",
    }:
        fail("Structured-output policy drifted")
    retry = profile["retry"]
    strict_expected_keys(
        retry,
        {
            "max_retries", "total_attempts", "backoff_seconds", "retryable",
            "non_retryable", "reuse_same_seed",
        },
        "$.semantic.runtime_profile.retry",
    )
    if profile["retry"]["total_attempts"] != profile["retry"]["max_retries"] + 1:
        fail("Retry attempt count mismatch")
    if len(profile["retry"]["backoff_seconds"]) != profile["retry"]["max_retries"]:
        fail("Retry backoff count mismatch")
    if not isinstance(retry["max_retries"], int) or isinstance(retry["max_retries"], bool) or retry["max_retries"] < 0:
        fail("max_retries must be a non-negative integer")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 for value in retry["backoff_seconds"]):
        fail("Retry backoff values must be non-negative numbers")
    if retry["retryable"] != ["connection_error", "timeout", "http_429", "http_5xx"]:
        fail("Retryable error policy drifted")
    if retry["non_retryable"] != ["http_4xx_except_429", "digest_mismatch", "schema_error"]:
        fail("Non-retryable error policy drifted")
    if profile["retry"]["reuse_same_seed"] is not True:
        fail("Retries must reuse the same seed")
    timeouts = profile["timeouts_seconds"]
    strict_expected_keys(timeouts, {"health", "model_metadata", "request"}, "$.semantic.runtime_profile.timeouts_seconds")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 for value in timeouts.values()):
        fail("Every timeout must be a positive number")

    envelope = semantic["context_envelope"]
    caps = envelope["component_caps"]
    expected_caps = {
        "system_instruction_tokens", "user_instruction_tokens",
        "evidence_or_raw_batch_tokens", "tool_and_response_schema_tokens",
        "structured_state_tokens", "chat_template_overhead_tokens",
        "reserved_output_tokens", "safety_margin_tokens",
    }
    strict_expected_keys(caps, expected_caps, "$.semantic.context_envelope.component_caps")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in caps.values()):
        fail("Every context component cap must be a positive integer")
    if sum(caps.values()) != envelope["hard_limit_tokens"]:
        fail("Context component caps do not close exactly to the hard limit")
    if envelope["hard_limit_tokens"] != options["num_ctx"]:
        fail("Context hard limit differs from num_ctx")
    if caps["reserved_output_tokens"] != options["num_predict"]:
        fail("Reserved output differs from num_predict")
    calculated_input_cap = (
        envelope["hard_limit_tokens"]
        - caps["reserved_output_tokens"]
        - caps["safety_margin_tokens"]
    )
    if envelope["input_cap_tokens"] != calculated_input_cap:
        fail("Context input cap arithmetic mismatch")

    accounting = semantic["token_accounting"]
    strict_expected_keys(
        accounting,
        {
            "tokenizer_identity", "authoritative_input_counter",
            "authoritative_output_counter", "component_counter",
            "pre_request_component_gate", "batch_budget_counter",
            "batch_budget_unit_rule", "include_all_success_failure_retry_attempts",
            "missing_provider_counter",
        },
        "$.semantic.token_accounting",
    )
    if accounting["authoritative_input_counter"] != "ollama.prompt_eval_count":
        fail("Unexpected authoritative input token counter")
    if accounting["authoritative_output_counter"] != "ollama.eval_count":
        fail("Unexpected authoritative output token counter")
    if accounting["missing_provider_counter"] != "unavailable_and_comparison_invalid":
        fail("Missing token counters must invalidate the comparison")
    if accounting["tokenizer_identity"] != "model_embedded_tokenizer_bound_by_full_manifest_digest":
        fail("Tokenizer identity policy drifted")
    if accounting["component_counter"] != "provider_prompt_eval_cumulative_prefix_differential_v1":
        fail("Component counter policy drifted")
    if accounting["pre_request_component_gate"] != "utf8_byte_upper_bound_v1":
        fail("Pre-request context gate policy drifted")
    if accounting["batch_budget_counter"] != "utf8_byte_upper_bound_v1" or accounting["batch_budget_unit_rule"] != "one_budget_unit_per_utf8_byte":
        fail("Batch budgeting policy drifted")
    if accounting["include_all_success_failure_retry_attempts"] is not True:
        fail("Every LLM attempt must be included in token accounting")
    if envelope["overflow_policy"] != "reject_before_runtime_inference":
        fail("Context overflow must fail before runtime inference")
    if envelope["component_counter_enforcement"] != "observed_prefix_deltas_each_fit_and_runtime_utf8_upper_bound_gate":
        fail("Context component enforcement policy drifted")
    threshold = envelope["high_water_probe_minimum_input_utilization"]
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0.75 <= threshold <= 1:
        fail("Context high-water threshold must remain within [0.75, 1]")

    evaluation = semantic["evaluation_contract"]
    strict_expected_keys(
        evaluation,
        {
            "arms", "shared_runtime_profile", "batch_manifest_artifact",
            "shared_selection_artifact", "batch_count", "selection_seed",
            "selection_algorithm", "selection_hash_domain", "selection_seed_encoding",
            "selection_preimage", "population", "freeze_before_any_llm_result",
            "replace_after_result_observation", "arm_difference_only",
            "proposed_handoff", "baseline_handoff", "evaluation_binding_stage",
        },
        "$.semantic.evaluation_contract",
    )
    if evaluation["arms"] != ["proposed", "baseline"]:
        fail("Evaluation arms changed")
    if evaluation["batch_count"] != 3:
        fail("The evaluation must bind exactly three batches")
    if evaluation["shared_runtime_profile"] != "#/semantic/runtime_profile":
        fail("Baseline and Proposed do not share the runtime profile")
    if evaluation["shared_selection_artifact"] != "artifacts/evaluation/selection.json":
        fail("Baseline and Proposed do not share the canonical selection artifact")
    if evaluation["batch_manifest_artifact"] != "artifacts/chunking/batch_manifest.jsonl":
        fail("Unexpected canonical Batch manifest path")
    if evaluation["selection_seed_encoding"] != "unsigned_base10_ascii":
        fail("Unexpected selection seed encoding")
    if evaluation["selection_preimage"] != "domain_utf8_nul_seed_ascii_nul_batch_id_utf8":
        fail("Unexpected Batch selection preimage contract")
    if evaluation["selection_seed"] != options["seed"]:
        fail("Batch selection seed must equal the locked runtime seed")
    if evaluation["selection_algorithm"] != "rank_ascending_sha256(domain_nul_seed_nul_batch_id),tie_break_batch_id":
        fail("Batch selection algorithm drifted")
    if evaluation["selection_hash_domain"] != "ai-sast-batch-selection-v1":
        fail("Batch selection hash domain drifted")
    if evaluation["population"] != "all_phase2_batches":
        fail("Batch selection population drifted")
    if evaluation["freeze_before_any_llm_result"] is not True:
        fail("Batch selection must be frozen before LLM results")
    if evaluation["replace_after_result_observation"] is not False:
        fail("Result-based Batch replacement must be forbidden")
    if evaluation["arm_difference_only"] != "handoff_payload_policy":
        fail("Evaluation arms differ in more than the handoff policy")
    if evaluation["proposed_handoff"] != "reference_only_evidence_pull" or evaluation["baseline_handoff"] != "repeat_full_raw_context":
        fail("Evaluation handoff policy drifted")
    if evaluation["evaluation_binding_stage"] != "phase2_after_batch_manifest":
        fail("Evaluation binding stage drifted")
    cache = semantic["cache_policy"]
    if cache != {
        "primary_comparison": "cold_llm_result_cache",
        "llm_result_cache_enabled": False,
        "chunk_index_retrieval_cache_metric": "separate_non_token_optimization",
    }:
        fail("Cache policy drifted")
    stage = semantic["lock_stage"]
    if stage == "preflight_locked" and "evaluation_binding" in semantic:
        fail("Phase 1 preflight lock must not contain a fabricated evaluation binding")
    if stage == "evaluation_frozen":
        binding = semantic.get("evaluation_binding")
        if not isinstance(binding, dict):
            fail("Frozen evaluation stage requires an evaluation binding")
        strict_expected_keys(
            binding,
            {
                "batch_manifest_path",
                "batch_manifest_sha256",
                "selection_path",
                "selection_sha256",
            },
            "$.semantic.evaluation_binding",
        )
        if binding["batch_manifest_path"] != evaluation["batch_manifest_artifact"]:
            fail("Frozen Batch manifest path differs from the selection contract")
        if binding["selection_path"] != evaluation["shared_selection_artifact"]:
            fail("Frozen selection path differs from the selection contract")
        for key in ("batch_manifest_sha256", "selection_sha256"):
            if not isinstance(binding.get(key), str) or DIGEST_RE.fullmatch(binding[key]) is None:
                fail(f"Frozen evaluation binding has no full digest: {key}")
    elif stage != "preflight_locked":
        fail(f"Unsupported experiment lock stage: {stage}")


def expected_context_probe_requests(
    *, model: str, options: dict[str, Any], keep_alive: str
) -> list[dict[str, Any]]:
    recipe = CONTEXT_RECIPE
    system = " ".join([recipe["system_marker"]] * recipe["system_repetitions"])
    user_instruction = " ".join(
        [recipe["user_instruction_marker"]] * recipe["user_instruction_repetitions"]
    ) + "\nReturn the JSON object with ok set to true. Do not call a tool."
    evidence = " ".join(
        [recipe["evidence_marker"]] * recipe["evidence_repetitions"]
    )
    state = " ".join(
        [recipe["structured_state_marker"]]
        * recipe["structured_state_repetitions"]
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "preflight_context_lookup",
                "description": " ".join(
                    [recipe["tool_description_marker"]]
                    * recipe["tool_description_repetitions"]
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"chunk_id": {"type": "string"}},
                    "required": ["chunk_id"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    parts = {
        "user": "[USER_INSTRUCTION]\n" + user_instruction,
        "evidence": "[EVIDENCE]\n" + evidence,
        "state": "[STRUCTURED_STATE]\n" + state,
        "schema": "[RESPONSE_SCHEMA]\n" + canonical_json(ENVELOPE_SCHEMA).decode("utf-8"),
    }
    messages = [
        [
            {"role": "system", "content": ""},
            {"role": "user", "content": ""},
        ],
        [
            {"role": "system", "content": system},
            {"role": "user", "content": ""},
        ],
        [
            {"role": "system", "content": system},
            {"role": "user", "content": parts["user"]},
        ],
        [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join([parts["user"], parts["evidence"]])},
        ],
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "\n".join([parts["user"], parts["evidence"], parts["state"]]),
            },
        ],
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "\n".join(
                    [parts["user"], parts["evidence"], parts["state"], parts["schema"]]
                ),
            },
        ],
    ]
    result: list[dict[str, Any]] = []
    for index, value in enumerate(messages):
        payload: dict[str, Any] = {
            "model": model,
            "messages": value,
            "format": ENVELOPE_SCHEMA,
            "options": options,
            "stream": False,
            "keep_alive": keep_alive,
        }
        if index == len(messages) - 1:
            payload["tools"] = tools
        result.append(payload)
    return result


def validate_artifacts(root: Path, lock: dict[str, Any]) -> None:
    entries = lock["evidence"]["artifacts"]
    if not isinstance(entries, list) or not entries:
        fail("Experiment lock has no evidence artifacts")
    names: set[str] = set()
    paths: set[str] = set()
    root_resolved = root.resolve()
    documents: dict[str, Any] = {}
    for entry in entries:
        name = entry["name"]
        relative = entry["path"]
        if name in names or relative in paths:
            fail("Duplicate evidence artifact name or path")
        names.add(name)
        paths.add(relative)
        path = (root / relative).resolve()
        if path != root_resolved and root_resolved not in path.parents:
            fail(f"Evidence artifact escapes the project root: {relative}")
        if not path.is_file():
            fail(f"Missing evidence artifact: {relative}")
        raw = path.read_bytes()
        if len(raw) != entry["size_bytes"]:
            fail(f"Evidence size mismatch: {relative}")
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            fail(f"Evidence SHA-256 mismatch: {relative}")
        documents[name] = load_json(path)

    required = {
        "ollama_version", "model_tags", "model_show", "structured_output_smoke",
        "context_envelope", "runtime_ps", "preflight_report",
    }
    if names != required:
        fail(f"Evidence artifact set mismatch: {sorted(names)}")

    semantic = lock["semantic"]
    if documents["ollama_version"]["response"]["version"] != semantic["backend"]["ollama_version"]:
        fail("Ollama version evidence mismatch")
    selected = documents["model_tags"]["selected_model"]
    model_contract = semantic["model"]
    if selected["name"] != model_contract["name"]:
        fail("Model name evidence mismatch")
    if selected["digest"] != model_contract["manifest_digest"]:
        fail("Model digest evidence mismatch")
    if selected["size"] != model_contract["manifest_size_bytes"]:
        fail("Model manifest size evidence mismatch")
    selected_details = selected["details"]
    for evidence_key, contract_key in (
        ("format", "format"),
        ("family", "family"),
        ("parameter_size", "parameter_size"),
        ("quantization_level", "quantization_level"),
    ):
        if selected_details[evidence_key] != model_contract[contract_key]:
            fail(f"Model detail evidence mismatch: {evidence_key}")
    if sorted(selected["capabilities"]) != model_contract["capabilities"]:
        fail("Model capability evidence mismatch")
    show = documents["model_show"]
    for detail_key in (
        "parent_model", "format", "family", "families", "parameter_size",
        "quantization_level",
    ):
        if show["details"].get(detail_key) != selected_details.get(detail_key):
            fail(f"Model show/tag detail evidence mismatch: {detail_key}")
    if sorted(show["capabilities"]) != model_contract["capabilities"]:
        fail("Model show capability evidence mismatch")
    template = show["template"]
    if not isinstance(template, str) or len(template) != show["template_length_characters"]:
        fail("Stored model template length mismatch")
    if hashlib.sha256(template.encode("utf-8")).hexdigest() != show["template_sha256"]:
        fail("Stored model template hash mismatch")
    if show["template_sha256"] != model_contract["chat_template_sha256"]:
        fail("Model template evidence mismatch")
    parameters = show["parameters"]
    if parameters is not None and not isinstance(parameters, str):
        fail("Stored model parameter block has an invalid type")
    parameter_text = parameters or ""
    if hashlib.sha256(parameter_text.encode("utf-8")).hexdigest() != show["parameters_sha256"]:
        fail("Stored model parameter hash mismatch")
    if show["parameters_sha256"] != model_contract["model_parameters_sha256"]:
        fail("Model parameter evidence mismatch")
    if show["parameters_empty"] != model_contract["model_parameters_empty"]:
        fail("Model parameter empty-state evidence mismatch")
    if show["model_info"].get(model_contract["native_context_key"]) != model_contract["native_context_length"]:
        fail("Native model context evidence mismatch")
    if show["tokenizer"] != model_contract["tokenizer"]:
        fail("Tokenizer evidence mismatch")

    smoke = documents["structured_output_smoke"]
    if smoke["status"] != "PASS" or smoke["same_seed_canonical_output_equal"] is not True:
        fail("Structured-output smoke did not pass")
    if len(smoke["responses"]) != 2 or len(smoke["parsed_outputs"]) != 2:
        fail("Structured-output smoke must contain two calls")
    if smoke["parsed_outputs"] != [EXPECTED_SMOKE, EXPECTED_SMOKE]:
        fail("Same-seed structured outputs differ")
    expected_smoke_request = {
        "model": model_contract["name"],
        "messages": SMOKE_MESSAGES,
        "format": SMOKE_SCHEMA,
        "options": semantic["runtime_profile"]["options"],
        "stream": False,
        "keep_alive": semantic["runtime_profile"]["transport"]["keep_alive"],
    }
    if smoke["request"] != expected_smoke_request:
        fail("Structured smoke request differs from the experiment lock")
    for index, response in enumerate(smoke["responses"]):
        if response.get("model") != model_contract["name"] or response.get("done") is not True:
            fail("Invalid structured smoke provider response")
        if response.get("done_reason") == "length":
            fail("Structured smoke output was truncated")
        if not isinstance(response.get("prompt_eval_count"), int) or isinstance(response.get("prompt_eval_count"), bool) or response["prompt_eval_count"] <= 0:
            fail("Structured smoke is missing prompt_eval_count")
        if not isinstance(response.get("eval_count"), int) or isinstance(response.get("eval_count"), bool) or response["eval_count"] <= 0:
            fail("Structured smoke is missing eval_count")
        content = response.get("message", {}).get("content")
        if not isinstance(content, str) or "```" in content:
            fail("Structured smoke content is missing or fenced")
        try:
            parsed = json.loads(content, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            fail(f"Stored structured smoke content is invalid: {exc}")
        if parsed != smoke["parsed_outputs"][index]:
            fail("Stored structured smoke content differs from parsed output")

    envelope = documents["context_envelope"]
    contract = semantic["context_envelope"]
    if envelope["status"] != "PASS" or envelope["component_cap_sum"] != contract["hard_limit_tokens"]:
        fail("Context envelope evidence did not pass")
    if envelope["component_caps"] != contract["component_caps"]:
        fail("Context envelope evidence differs from the lock")
    probe = envelope["probe"]
    if probe["status"] != "PASS" or probe["method"] != "provider_prompt_eval_cumulative_prefix_differential_v1":
        fail("Context prefix-differential method mismatch")
    if probe["recipe"] != CONTEXT_RECIPE or probe["response_schema"] != ENVELOPE_SCHEMA:
        fail("Context prefix-differential recipe/schema mismatch")
    expected_requests = expected_context_probe_requests(
        model=model_contract["name"],
        options=semantic["runtime_profile"]["options"],
        keep_alive=semantic["runtime_profile"]["transport"]["keep_alive"],
    )
    observations = probe["observations"]
    component_order = [
        "chat_template_overhead_tokens",
        "system_instruction_tokens",
        "user_instruction_tokens",
        "evidence_or_raw_batch_tokens",
        "structured_state_tokens",
        "tool_and_response_schema_tokens",
    ]
    if len(observations) != len(component_order):
        fail("Context prefix-differential observation count mismatch")
    previous_count = 0
    recalculated_components: dict[str, int] = {}
    for index, (observation, component, expected_request) in enumerate(
        zip(observations, component_order, expected_requests)
    ):
        if observation["component"] != component:
            fail("Context prefix-differential component order mismatch")
        if observation["request"] != expected_request:
            fail(f"Context prefix request mismatch at observation {index}")
        if observation["request_sha256"] != hashlib.sha256(canonical_json(expected_request)).hexdigest():
            fail(f"Context prefix request hash mismatch at observation {index}")
        response = observation["provider_response"]
        if response.get("model") != model_contract["name"] or response.get("done") is not True:
            fail("Context prefix provider response is incomplete or from another model")
        if response.get("done_reason") == "length":
            fail("Context prefix provider response was truncated")
        prompt_count = response.get("prompt_eval_count")
        output_count = response.get("eval_count")
        if not isinstance(prompt_count, int) or isinstance(prompt_count, bool) or prompt_count <= 0:
            fail("Context prefix input counter is invalid")
        if not isinstance(output_count, int) or isinstance(output_count, bool) or output_count <= 0:
            fail("Context prefix output counter is invalid")
        if output_count > contract["component_caps"]["reserved_output_tokens"]:
            fail("Context prefix output exceeds the reservation")
        content = response.get("message", {}).get("content")
        try:
            parsed = json.loads(content, object_pairs_hook=reject_duplicate_keys)
        except (TypeError, json.JSONDecodeError) as exc:
            fail(f"Context prefix structured response is invalid: {exc}")
        if parsed != EXPECTED_ENVELOPE or observation["parsed_output"] != EXPECTED_ENVELOPE:
            fail("Context prefix structured response differs from the schema")
        delta = prompt_count - previous_count
        if delta < 0 or observation["component_delta_tokens"] != delta:
            fail("Context prefix component delta mismatch")
        if observation["cumulative_prompt_eval_count"] != prompt_count:
            fail("Context prefix cumulative counter mismatch")
        if delta > contract["component_caps"][component]:
            fail(f"Context prefix component exceeds cap: {component}")
        recalculated_components[component] = delta
        previous_count = prompt_count
    if probe["component_counts"] != recalculated_components:
        fail("Context prefix component summary mismatch")
    if probe["component_count_sum"] != sum(recalculated_components.values()):
        fail("Context prefix component reconciliation mismatch")
    count = previous_count
    if probe["provider_prompt_eval_count"] != count or probe["component_count_sum"] != count:
        fail("Context prefix provider total does not reconcile")
    if probe["input_cap_tokens"] != contract["input_cap_tokens"]:
        fail("Context prefix input cap differs from the lock")
    if count > contract["input_cap_tokens"]:
        fail("Context probe exceeded the input cap")
    utilization = count / contract["input_cap_tokens"]
    if probe["input_cap_utilization"] != utilization:
        fail("Context prefix utilization summary mismatch")
    if utilization < contract["high_water_probe_minimum_input_utilization"]:
        fail("Context probe did not exercise the locked high-water threshold")

    running = documents["runtime_ps"]["loaded_model"]
    if running["digest"] != semantic["model"]["manifest_digest"]:
        fail("Loaded model digest evidence mismatch")
    if running["context_length"] != semantic["runtime_profile"]["options"]["num_ctx"]:
        fail("Effective context length evidence mismatch")
    report = documents["preflight_report"]
    if report["status"] != "PASS":
        fail("Preflight report status is not PASS")
    preflight_sha256, preflight_id = preflight_stage_identity(semantic)
    if report["experiment_id"] != preflight_id:
        fail("Historical preflight report experiment ID mismatch")
    if report["semantic_sha256"] != preflight_sha256:
        fail("Historical preflight report semantic hash mismatch")


def validate_evaluation_binding(root: Path, lock: dict[str, Any]) -> None:
    semantic = lock["semantic"]
    if semantic["lock_stage"] == "preflight_locked":
        return
    binding = semantic["evaluation_binding"]
    contract = semantic["evaluation_contract"]
    root_resolved = root.resolve()

    def artifact_path(relative: str) -> Path:
        value = (root / relative).resolve()
        if value != root_resolved and root_resolved not in value.parents:
            fail(f"Evaluation artifact escapes the project root: {relative}")
        if not value.is_file():
            fail(f"Frozen evaluation artifact is missing: {relative}")
        return value

    batch_path = artifact_path(binding["batch_manifest_path"])
    selection_path = artifact_path(binding["selection_path"])
    if hashlib.sha256(batch_path.read_bytes()).hexdigest() != binding["batch_manifest_sha256"]:
        fail("Frozen Batch manifest SHA-256 mismatch")
    if hashlib.sha256(selection_path.read_bytes()).hexdigest() != binding["selection_sha256"]:
        fail("Frozen selection artifact SHA-256 mismatch")

    batches = load_jsonl(batch_path)
    batch_ids = [item.get("batch_id") for item in batches]
    if not batch_ids or any(not isinstance(value, str) or not value for value in batch_ids):
        fail("Every frozen Batch manifest record must have a non-empty batch_id")
    if len(batch_ids) != len(set(batch_ids)):
        fail("Frozen Batch manifest has duplicate batch_id values")

    selection = load_json(selection_path)
    expected_selection_keys = {
        "schema_version",
        "target_commit_sha",
        "target_scope_sha256",
        "batch_manifest_path",
        "batch_manifest_sha256",
        "selection_seed",
        "selection_seed_encoding",
        "selection_algorithm",
        "selection_hash_domain",
        "batch_count",
        "selected_batch_ids",
        "frozen_before_any_llm_result",
        "replace_after_result_observation",
    }
    strict_expected_keys(selection, expected_selection_keys, "$.selection")
    if selection["schema_version"] != 1:
        fail("Unsupported selection artifact schema version")
    target = semantic["target_binding"]
    expected_values = {
        "target_commit_sha": target["target_commit_sha"],
        "target_scope_sha256": target["target_scope_sha256"],
        "batch_manifest_path": binding["batch_manifest_path"],
        "batch_manifest_sha256": binding["batch_manifest_sha256"],
        "selection_seed": contract["selection_seed"],
        "selection_seed_encoding": contract["selection_seed_encoding"],
        "selection_algorithm": contract["selection_algorithm"],
        "selection_hash_domain": contract["selection_hash_domain"],
        "batch_count": contract["batch_count"],
        "frozen_before_any_llm_result": True,
        "replace_after_result_observation": False,
    }
    for key, expected in expected_values.items():
        if selection[key] != expected:
            fail(f"Frozen selection contract mismatch: {key}")
    selected_ids = selection["selected_batch_ids"]
    if not isinstance(selected_ids, list) or len(selected_ids) != contract["batch_count"]:
        fail("Frozen selection must contain exactly three Batch IDs")
    if any(not isinstance(value, str) or not value for value in selected_ids):
        fail("Frozen selected Batch IDs must be non-empty strings")
    if len(selected_ids) != len(set(selected_ids)):
        fail("Frozen selected Batch IDs must be unique")
    if not set(selected_ids).issubset(set(batch_ids)):
        fail("Frozen selection references a Batch outside the manifest")
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
    if selected_ids != ranked[: contract["batch_count"]]:
        fail("Frozen Batch IDs do not match the deterministic ranking")
    if "experiment_id" in selection:
        fail("Selection artifact must not contain the final Experiment ID")


def http_json(url: str, *, payload: dict[str, Any] | None = None, timeout: float) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        body = canonical_json(payload)
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = request.Request(url, data=body, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(value, dict):
        fail(f"Live API did not return an object: {url}")
    return value


def tokenizer_fingerprint_from_show(show: dict[str, Any]) -> dict[str, Any]:
    model_info = show.get("model_info")
    if not isinstance(model_info, dict):
        fail("Live verbose model metadata has no model_info object")
    tokenizer = {
        key: value for key, value in model_info.items() if key.startswith("tokenizer.")
    }
    tokens = tokenizer.get("tokenizer.ggml.tokens")
    merges = tokenizer.get("tokenizer.ggml.merges")
    if not isinstance(tokens, list) or not tokens:
        fail("Live tokenizer vocabulary is missing")
    if not isinstance(merges, list) or not merges:
        fail("Live tokenizer merges are missing")
    return {
        "binding": "model_embedded_tokenizer_bound_by_manifest_digest",
        "metadata_sha256": hashlib.sha256(
            b"ai-sast-ollama-tokenizer-metadata-v1\0" + canonical_json(tokenizer)
        ).hexdigest(),
        "metadata_keys": sorted(tokenizer),
        "vocabulary_entries": len(tokens),
        "merge_entries": len(merges),
    }


def live_verify(root: Path, lock: dict[str, Any], *, full_context: bool) -> None:
    semantic = lock["semantic"]
    base_url = semantic["backend"]["base_url"]
    timeouts = semantic["runtime_profile"]["timeouts_seconds"]
    version = http_json(base_url + "/api/version", timeout=timeouts["health"])
    if version.get("version") != semantic["backend"]["ollama_version"]:
        fail("Live Ollama version differs from the lock")
    tags = http_json(base_url + "/api/tags", timeout=timeouts["model_metadata"])
    matches = [item for item in tags.get("models", []) if item.get("name") == semantic["model"]["name"]]
    if len(matches) != 1 or matches[0].get("digest") != semantic["model"]["manifest_digest"]:
        fail("Live model name/digest differs from the lock")
    live_model = matches[0]
    if live_model.get("size") != semantic["model"]["manifest_size_bytes"]:
        fail("Live model manifest size differs from the lock")
    live_details = live_model.get("details", {})
    for evidence_key, contract_key in (
        ("format", "format"),
        ("family", "family"),
        ("parameter_size", "parameter_size"),
        ("quantization_level", "quantization_level"),
    ):
        if live_details.get(evidence_key) != semantic["model"][contract_key]:
            fail(f"Live model detail differs from the lock: {evidence_key}")
    if sorted(live_model.get("capabilities", [])) != semantic["model"]["capabilities"]:
        fail("Live model capabilities differ from the lock")
    show = http_json(
        base_url + "/api/show",
        payload={"model": semantic["model"]["name"], "verbose": False},
        timeout=timeouts["request"],
    )
    template = show.get("template")
    if not isinstance(template, str) or hashlib.sha256(template.encode("utf-8")).hexdigest() != semantic["model"]["chat_template_sha256"]:
        fail("Live model template differs from the lock")
    parameters = show.get("parameters") or ""
    if not isinstance(parameters, str) or hashlib.sha256(parameters.encode("utf-8")).hexdigest() != semantic["model"]["model_parameters_sha256"]:
        fail("Live model parameter block differs from the lock")
    if show.get("model_info", {}).get(semantic["model"]["native_context_key"]) != semantic["model"]["native_context_length"]:
        fail("Live native model context differs from the lock")
    verbose_show = http_json(
        base_url + "/api/show",
        payload={"model": semantic["model"]["name"], "verbose": True},
        timeout=timeouts["request"],
    )
    if tokenizer_fingerprint_from_show(verbose_show) != semantic["model"]["tokenizer"]:
        fail("Live tokenizer fingerprint differs from the lock")

    smoke = load_json(root / "artifacts/preflight/structured_output_smoke.json")
    response = http_json(
        base_url + "/api/chat",
        payload=smoke["request"],
        timeout=timeouts["request"],
    )
    if response.get("done") is not True or response.get("done_reason") == "length":
        fail("Live structured smoke did not complete")
    if response.get("model") != semantic["model"]["name"]:
        fail("Live structured smoke model differs from the lock")
    content = response.get("message", {}).get("content")
    try:
        parsed = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except (TypeError, json.JSONDecodeError) as exc:
        fail(f"Live structured smoke content is invalid: {exc}")
    if parsed != smoke["parsed_outputs"][0]:
        fail("Live structured smoke output differs from the locked output")
    if not isinstance(response.get("prompt_eval_count"), int) or response["prompt_eval_count"] <= 0:
        fail("Live smoke input token counter is missing")
    if not isinstance(response.get("eval_count"), int) or response["eval_count"] <= 0:
        fail("Live smoke output token counter is missing")

    if full_context:
        envelope = load_json(root / "artifacts/preflight/context_envelope.json")
        previous_count = 0
        for observation in envelope["probe"]["observations"]:
            live_response = http_json(
                base_url + "/api/chat",
                payload=observation["request"],
                timeout=timeouts["request"],
            )
            if live_response.get("model") != semantic["model"]["name"] or live_response.get("done") is not True:
                fail("Live full context probe returned an incomplete response")
            if live_response.get("done_reason") == "length":
                fail("Live full context probe response was truncated")
            prompt_count = live_response.get("prompt_eval_count")
            if not isinstance(prompt_count, int) or isinstance(prompt_count, bool):
                fail("Live full context probe has no input counter")
            if prompt_count != observation["cumulative_prompt_eval_count"]:
                fail("Live full context probe counter differs from stored evidence")
            if prompt_count - previous_count != observation["component_delta_tokens"]:
                fail("Live full context probe component delta differs from stored evidence")
            content = live_response.get("message", {}).get("content")
            try:
                parsed_live = json.loads(content, object_pairs_hook=reject_duplicate_keys)
            except (TypeError, json.JSONDecodeError) as exc:
                fail(f"Live full context probe output is invalid: {exc}")
            if parsed_live != EXPECTED_ENVELOPE:
                fail("Live full context probe structured output differs from the schema")
            previous_count = prompt_count

    running = http_json(base_url + "/api/ps", timeout=timeouts["model_metadata"])
    matches = [item for item in running.get("models", []) if item.get("name") == semantic["model"]["name"]]
    if len(matches) != 1:
        fail("Live locked model is not loaded")
    if matches[0].get("digest") != semantic["model"]["manifest_digest"]:
        fail("Live loaded model digest differs from the lock")
    if matches[0].get("context_length") != semantic["runtime_profile"]["options"]["num_ctx"]:
        fail("Live effective num_ctx differs from the lock")


def verify(root: Path, *, live: bool, live_full: bool = False) -> dict[str, Any]:
    lock_path = root / "experiment.lock.yaml"
    schema_path = root / "schemas" / "experiment-lock.schema.json"
    lock = load_json(lock_path)
    schema = load_json(schema_path)
    assert_no_placeholders(lock)
    assert_no_placeholders(schema)
    validate_schema(lock, schema)
    target_lock = load_json(root / "target.lock.yaml")
    validate_semantics(lock, target_lock)
    validate_artifacts(root, lock)
    validate_evaluation_binding(root, lock)
    if live or live_full:
        live_verify(root, lock, full_context=live_full)
    return lock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--live-full", action="store_true")
    arguments = parser.parse_args()
    try:
        lock = verify(
            arguments.project_root.resolve(),
            live=arguments.live,
            live_full=arguments.live_full,
        )
    except (VerificationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print("PASS independent Phase 1 experiment lock verification")
    print(f"experiment_id={lock['identity']['experiment_id']}")
    print(f"semantic_sha256={lock['identity']['semantic_sha256']}")
    print(f"live_check={'PASS' if arguments.live or arguments.live_full else 'SKIPPED'}")
    print(f"live_full_context={'PASS' if arguments.live_full else 'SKIPPED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
