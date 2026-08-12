#!/usr/bin/env python3
"""Run the Phase 1 Ollama preflight and seal the experiment contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.providers import OllamaProvider, ProviderError  # noqa: E402


HASH_DOMAIN = b"ai-sast-experiment-semantic-v1\0"
EXPERIMENT_ID_PREFIX = "exp-v1-"
TARGET_COMMIT = "a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976"
TARGET_SCOPE_SHA256 = "2d7910c98cd57192f9c80436d18d39be247a8b8e6705b98f831ee747b1d1d50a"
TARGET_FILE_LIST_SHA256 = "fb00da1a82fcd1e02b436c4f58cbd8391094901da8450b9881063db3558e6057"
DIGEST_RE = re.compile(r"[0-9a-f]{64}")

SMOKE_SCHEMA: dict[str, Any] = {
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
    {
        "role": "system",
        "content": "Return only JSON matching the provided schema.",
    },
    {
        "role": "user",
        "content": (
            "Preflight check. Set status to OK, backend to ollama, "
            "and seed_echo to 20260811."
        ),
    },
]
EXPECTED_SMOKE = {"status": "OK", "backend": "ollama", "seed_echo": 20260811}

ENVELOPE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "enum": [True]}},
    "required": ["ok"],
    "additionalProperties": False,
}
EXPECTED_ENVELOPE_RESPONSE = {"ok": True}

CONTEXT_COMPONENT_CAPS = {
    "system_instruction_tokens": 768,
    "user_instruction_tokens": 256,
    "evidence_or_raw_batch_tokens": 8192,
    "tool_and_response_schema_tokens": 768,
    "structured_state_tokens": 256,
    "chat_template_overhead_tokens": 256,
    "reserved_output_tokens": 1536,
    "safety_margin_tokens": 4352,
}


class PreflightError(RuntimeError):
    """A fail-closed Phase 1 contract error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def semantic_identity(semantic: dict[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(HASH_DOMAIN + canonical_json(semantic)).hexdigest()
    return digest, EXPERIMENT_ID_PREFIX + digest[:24]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pretty_json(value))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"Cannot read strict JSON from {path}: {exc}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreflightError(message)


def selected_model(tags: dict[str, Any], model_name: str) -> dict[str, Any]:
    models = tags.get("models")
    require(isinstance(models, list), "/api/tags response has no models array")
    matches = [item for item in models if isinstance(item, dict) and item.get("name") == model_name]
    require(len(matches) == 1, f"Expected exactly one installed model named {model_name!r}")
    model = matches[0]
    digest = model.get("digest")
    require(isinstance(digest, str) and DIGEST_RE.fullmatch(digest) is not None,
            "Installed model does not expose a full 64-hex manifest digest")
    return model


def model_context_length(show: dict[str, Any]) -> tuple[str, int]:
    model_info = show.get("model_info")
    require(isinstance(model_info, dict), "/api/show response has no model_info object")
    matches = [
        (key, value)
        for key, value in model_info.items()
        if key.endswith(".context_length")
        and isinstance(value, int)
        and not isinstance(value, bool)
    ]
    require(len(matches) == 1, f"Expected one model context length, got {matches!r}")
    return matches[0]


def strict_smoke_content(response: dict[str, Any], model_name: str) -> dict[str, Any]:
    require(response.get("model") == model_name, "Smoke response model differs from the lock")
    require(response.get("done") is True, "Smoke response is not complete")
    require(response.get("done_reason") != "length", "Smoke response was truncated")
    message = response.get("message")
    require(isinstance(message, dict), "Smoke response has no message object")
    require(message.get("role") == "assistant", "Smoke response role is not assistant")
    content = message.get("content")
    require(isinstance(content, str) and content.strip(), "Smoke response content is empty")
    require("```" not in content, "Smoke response used a Markdown fence")
    try:
        value = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise PreflightError(f"Smoke content is not strict JSON: {exc}") from exc
    require(value == EXPECTED_SMOKE, f"Smoke content does not match the exact schema: {value!r}")
    for counter in ("prompt_eval_count", "eval_count"):
        require(
            isinstance(response.get(counter), int)
            and not isinstance(response.get(counter), bool)
            and response[counter] > 0,
            f"Smoke response has no positive {counter}",
        )
    return value


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PreflightError(f"Duplicate JSON key in model content: {key}")
        value[key] = item
    return value


def tokenizer_fingerprint(verbose_show: dict[str, Any]) -> dict[str, Any]:
    model_info = verbose_show.get("model_info")
    require(isinstance(model_info, dict), "Verbose /api/show has no model_info")
    tokenizer = {
        key: value for key, value in model_info.items() if key.startswith("tokenizer.")
    }
    require(tokenizer, "Verbose /api/show did not expose tokenizer metadata")
    tokens = tokenizer.get("tokenizer.ggml.tokens")
    merges = tokenizer.get("tokenizer.ggml.merges")
    require(isinstance(tokens, list) and len(tokens) > 0, "Tokenizer vocabulary is missing")
    require(isinstance(merges, list) and len(merges) > 0, "Tokenizer merges are missing")
    return {
        "binding": "model_embedded_tokenizer_bound_by_manifest_digest",
        "metadata_sha256": sha256_bytes(
            b"ai-sast-ollama-tokenizer-metadata-v1\0" + canonical_json(tokenizer)
        ),
        "metadata_keys": sorted(tokenizer),
        "vocabulary_entries": len(tokens),
        "merge_entries": len(merges),
    }


def runtime_options(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "num_ctx": arguments.num_ctx,
        "num_predict": arguments.num_predict,
        "temperature": arguments.temperature,
        "seed": arguments.seed,
        "top_k": arguments.top_k,
        "top_p": arguments.top_p,
        "min_p": arguments.min_p,
        "repeat_last_n": arguments.repeat_last_n,
        "repeat_penalty": arguments.repeat_penalty,
        "stop": [],
    }


def context_probe(
    provider: OllamaProvider,
    options: dict[str, Any],
    keep_alive: str,
) -> dict[str, Any]:
    recipe = {
        "system_marker": "s",
        "system_repetitions": 720,
        "user_instruction_marker": "u",
        "user_instruction_repetitions": 200,
        "evidence_marker": "e",
        "evidence_repetitions": 6500,
        "structured_state_marker": "q",
        "structured_state_repetitions": 220,
        "tool_description_marker": "t",
        "tool_description_repetitions": 350,
    }
    system = " ".join([recipe["system_marker"]] * recipe["system_repetitions"])
    user_instruction = " ".join(
        [recipe["user_instruction_marker"]] * recipe["user_instruction_repetitions"]
    ) + "\nReturn the JSON object with ok set to true. Do not call a tool."
    evidence = " ".join(
        [recipe["evidence_marker"]] * recipe["evidence_repetitions"]
    )
    structured_state = " ".join(
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
    response_schema_text = canonical_json(ENVELOPE_RESPONSE_SCHEMA).decode("utf-8")
    user_parts = {
        "user_instruction_tokens": "[USER_INSTRUCTION]\n" + user_instruction,
        "evidence_or_raw_batch_tokens": "[EVIDENCE]\n" + evidence,
        "structured_state_tokens": "[STRUCTURED_STATE]\n" + structured_state,
        "tool_and_response_schema_tokens": "[RESPONSE_SCHEMA]\n" + response_schema_text,
    }
    stages = [
        {
            "name": "chat_template_overhead_tokens",
            "messages": [
                {"role": "system", "content": ""},
                {"role": "user", "content": ""},
            ],
            "tools": None,
        },
        {
            "name": "system_instruction_tokens",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": ""},
            ],
            "tools": None,
        },
        {
            "name": "user_instruction_tokens",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_parts["user_instruction_tokens"]},
            ],
            "tools": None,
        },
        {
            "name": "evidence_or_raw_batch_tokens",
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            user_parts["user_instruction_tokens"],
                            user_parts["evidence_or_raw_batch_tokens"],
                        ]
                    ),
                },
            ],
            "tools": None,
        },
        {
            "name": "structured_state_tokens",
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            user_parts["user_instruction_tokens"],
                            user_parts["evidence_or_raw_batch_tokens"],
                            user_parts["structured_state_tokens"],
                        ]
                    ),
                },
            ],
            "tools": None,
        },
        {
            "name": "tool_and_response_schema_tokens",
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            user_parts["user_instruction_tokens"],
                            user_parts["evidence_or_raw_batch_tokens"],
                            user_parts["structured_state_tokens"],
                            user_parts["tool_and_response_schema_tokens"],
                        ]
                    ),
                },
            ],
            "tools": tools,
        },
    ]

    observations: list[dict[str, Any]] = []
    previous_prompt_count = 0
    component_counts: dict[str, int] = {}
    for stage in stages:
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": stage["messages"],
            "format": ENVELOPE_RESPONSE_SCHEMA,
            "options": options,
            "stream": False,
            "keep_alive": keep_alive,
        }
        if stage["tools"] is not None:
            payload["tools"] = stage["tools"]
        response = provider.chat(
            messages=stage["messages"],
            response_schema=ENVELOPE_RESPONSE_SCHEMA,
            options=options,
            stream=False,
            keep_alive=keep_alive,
            tools=stage["tools"],
        )
        parsed = strict_envelope_content(response, provider.model)
        prompt_count = response["prompt_eval_count"]
        delta = prompt_count - previous_prompt_count
        require(delta >= 0, f"Context prefix counter decreased at {stage['name']}")
        component_counts[stage["name"]] = delta
        observations.append(
            {
                "component": stage["name"],
                "request": payload,
                "request_sha256": sha256_bytes(canonical_json(payload)),
                "provider_response": response,
                "parsed_output": parsed,
                "cumulative_prompt_eval_count": prompt_count,
                "component_delta_tokens": delta,
            }
        )
        previous_prompt_count = prompt_count

    input_cap = sum(
        value
        for key, value in CONTEXT_COMPONENT_CAPS.items()
        if key not in {"reserved_output_tokens", "safety_margin_tokens"}
    )
    for name, count in component_counts.items():
        require(
            count <= CONTEXT_COMPONENT_CAPS[name],
            f"Context component exceeded cap: {name} {count} > {CONTEXT_COMPONENT_CAPS[name]}",
        )
    require(sum(component_counts.values()) == previous_prompt_count,
            "Context component deltas do not reconcile to provider input total")
    require(previous_prompt_count <= input_cap,
            f"Context-envelope probe exceeded the input cap: {previous_prompt_count} > {input_cap}")
    require(previous_prompt_count * 4 >= input_cap * 3,
            f"Context-envelope probe exercised less than 75% of the input cap: {previous_prompt_count}")
    require(all(
        item["provider_response"]["eval_count"]
        <= CONTEXT_COMPONENT_CAPS["reserved_output_tokens"]
        for item in observations
    ), "Context-envelope probe exceeded the output reservation")
    return {
        "status": "PASS",
        "method": "provider_prompt_eval_cumulative_prefix_differential_v1",
        "recipe": recipe,
        "response_schema": ENVELOPE_RESPONSE_SCHEMA,
        "component_counts": component_counts,
        "component_count_sum": sum(component_counts.values()),
        "provider_prompt_eval_count": previous_prompt_count,
        "input_cap_tokens": input_cap,
        "input_cap_utilization": previous_prompt_count / input_cap,
        "observations": observations,
    }


def strict_envelope_content(response: dict[str, Any], model_name: str) -> dict[str, Any]:
    require(response.get("model") == model_name, "Envelope response model differs from the lock")
    require(response.get("done") is True, "Envelope response is not complete")
    require(response.get("done_reason") != "length", "Envelope response was truncated")
    message = response.get("message")
    require(isinstance(message, dict) and message.get("role") == "assistant",
            "Envelope response has no assistant message")
    content = message.get("content")
    require(isinstance(content, str) and content.strip(), "Envelope response is empty")
    require("```" not in content, "Envelope response used a Markdown fence")
    try:
        parsed = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise PreflightError(f"Envelope response content is invalid JSON: {exc}") from exc
    require(parsed == EXPECTED_ENVELOPE_RESPONSE,
            f"Envelope response differs from schema: {parsed!r}")
    for counter in ("prompt_eval_count", "eval_count"):
        require(isinstance(response.get(counter), int)
                and not isinstance(response.get(counter), bool)
                and response[counter] > 0,
                f"Envelope response has no positive {counter}")
    return parsed


def running_model_receipt(
    running: dict[str, Any], model_name: str, digest: str, num_ctx: int
) -> dict[str, Any]:
    models = running.get("models")
    require(isinstance(models, list), "/api/ps response has no models array")
    matches = [item for item in models if isinstance(item, dict) and item.get("name") == model_name]
    require(len(matches) == 1, "The locked model is not loaded exactly once")
    model = matches[0]
    require(model.get("digest") == digest, "Loaded model digest differs from the lock")
    require(model.get("context_length") == num_ctx,
            "Loaded model context length differs from runtime num_ctx")
    size_vram = model.get("size_vram")
    require(isinstance(size_vram, int) and size_vram >= 0,
            "Loaded model has no valid size_vram")
    return {
        "name": model_name,
        "digest": digest,
        "size_bytes": model.get("size"),
        "size_vram_bytes": size_vram,
        "processor": "cpu" if size_vram == 0 else "mixed_or_gpu",
        "context_length": model.get("context_length"),
    }


def build_semantic(
    *,
    arguments: argparse.Namespace,
    ollama_version: str,
    installed: dict[str, Any],
    show: dict[str, Any],
    tokenizer: dict[str, Any],
) -> dict[str, Any]:
    details = installed.get("details")
    require(isinstance(details, dict), "Installed model has no details object")
    context_key, native_context = model_context_length(show)
    require(arguments.num_ctx <= native_context,
            f"num_ctx {arguments.num_ctx} exceeds model context {native_context}")
    digest = installed["digest"]
    template = show.get("template")
    require(isinstance(template, str) and template, "Model chat template is empty")
    parameters = show.get("parameters")
    require(parameters is None or isinstance(parameters, str),
            "Unexpected model parameters representation")
    caps = dict(CONTEXT_COMPONENT_CAPS)
    require(sum(caps.values()) == arguments.num_ctx,
            "Context component caps must sum exactly to num_ctx")
    require(caps["reserved_output_tokens"] == arguments.num_predict,
            "reserved_output_tokens must equal num_predict")

    return {
        "lock_stage": "preflight_locked",
        "target_binding": {
            "target_commit_sha": TARGET_COMMIT,
            "target_scope_sha256": TARGET_SCOPE_SHA256,
            "target_file_list_sha256": TARGET_FILE_LIST_SHA256,
        },
        "backend": {
            "provider": "ollama",
            "base_url": arguments.base_url.rstrip("/"),
            "ollama_version": ollama_version,
            "api_routes": {
                "version": "/api/version",
                "tags": "/api/tags",
                "show": "/api/show",
                "chat": "/api/chat",
                "running_models": "/api/ps",
            },
        },
        "model": {
            "name": arguments.model,
            "manifest_digest_algorithm": "sha256",
            "manifest_digest": digest,
            "manifest_digest_urn": "sha256:" + digest,
            "manifest_size_bytes": installed.get("size"),
            "format": details.get("format"),
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "quantization_level": details.get("quantization_level"),
            "native_context_length": native_context,
            "native_context_key": context_key,
            "capabilities": sorted(installed.get("capabilities", [])),
            "chat_template_sha256": sha256_bytes(template.encode("utf-8")),
            "model_parameters_sha256": sha256_bytes(
                (parameters or "").encode("utf-8")
            ),
            "model_parameters_empty": not bool(parameters),
            "tokenizer": tokenizer,
        },
        "runtime_profile": {
            "transport": {
                "stream": False,
                "keep_alive": arguments.keep_alive,
                "stateless_per_call": True,
                "max_concurrency": 1,
                "loopback_only": True,
                "max_response_bytes": 16777216,
            },
            "options": runtime_options(arguments),
            "structured_output": {
                "method": "ollama_chat_format_json_schema",
                "local_strict_json_validation": True,
                "additional_properties_default": "reject",
            },
            "retry": {
                "max_retries": arguments.max_retries,
                "total_attempts": arguments.max_retries + 1,
                "backoff_seconds": arguments.retry_backoff_seconds,
                "retryable": ["connection_error", "timeout", "http_429", "http_5xx"],
                "non_retryable": ["http_4xx_except_429", "digest_mismatch", "schema_error"],
                "reuse_same_seed": True,
            },
            "timeouts_seconds": {
                "health": arguments.health_timeout,
                "model_metadata": arguments.model_timeout,
                "request": arguments.request_timeout,
            },
        },
        "token_accounting": {
            "tokenizer_identity": "model_embedded_tokenizer_bound_by_full_manifest_digest",
            "authoritative_input_counter": "ollama.prompt_eval_count",
            "authoritative_output_counter": "ollama.eval_count",
            "component_counter": "provider_prompt_eval_cumulative_prefix_differential_v1",
            "pre_request_component_gate": "utf8_byte_upper_bound_v1",
            "batch_budget_counter": "utf8_byte_upper_bound_v1",
            "batch_budget_unit_rule": "one_budget_unit_per_utf8_byte",
            "include_all_success_failure_retry_attempts": True,
            "missing_provider_counter": "unavailable_and_comparison_invalid",
        },
        "context_envelope": {
            "hard_limit_tokens": arguments.num_ctx,
            "component_caps": caps,
            "input_cap_tokens": (
                arguments.num_ctx
                - caps["reserved_output_tokens"]
                - caps["safety_margin_tokens"]
            ),
            "overflow_policy": "reject_before_runtime_inference",
            "component_counter_enforcement": (
                "observed_prefix_deltas_each_fit_and_runtime_utf8_upper_bound_gate"
            ),
            "high_water_probe_minimum_input_utilization": 0.75,
        },
        "evaluation_contract": {
            "arms": ["proposed", "baseline"],
            "shared_runtime_profile": "#/semantic/runtime_profile",
            "batch_manifest_artifact": "artifacts/chunking/batch_manifest.jsonl",
            "shared_selection_artifact": "artifacts/evaluation/selection.json",
            "batch_count": 3,
            "selection_seed": arguments.seed,
            "selection_algorithm": (
                "rank_ascending_sha256(domain_nul_seed_nul_batch_id),tie_break_batch_id"
            ),
            "selection_hash_domain": "ai-sast-batch-selection-v1",
            "selection_seed_encoding": "unsigned_base10_ascii",
            "selection_preimage": "domain_utf8_nul_seed_ascii_nul_batch_id_utf8",
            "population": "all_phase2_batches",
            "freeze_before_any_llm_result": True,
            "replace_after_result_observation": False,
            "arm_difference_only": "handoff_payload_policy",
            "proposed_handoff": "reference_only_evidence_pull",
            "baseline_handoff": "repeat_full_raw_context",
            "evaluation_binding_stage": "phase2_after_batch_manifest",
        },
        "cache_policy": {
            "primary_comparison": "cold_llm_result_cache",
            "llm_result_cache_enabled": False,
            "chunk_index_retrieval_cache_metric": "separate_non_token_optimization",
        },
    }


def artifact_entry(root: Path, name: str, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    return {
        "name": name,
        "path": relative_path.replace("\\", "/"),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    root = arguments.project_root.resolve()
    target_lock = read_json(root / "target.lock.yaml")
    require(target_lock["target"]["commit_sha"] == TARGET_COMMIT,
            "Phase 0 target commit differs from the Phase 1 binding")
    require(target_lock["analysis_scope"]["scope_sha256"] == TARGET_SCOPE_SHA256,
            "Phase 0 target scope differs from the Phase 1 binding")
    require(
        target_lock["analysis_scope"]["target_file_list_sha256"]
        == TARGET_FILE_LIST_SHA256,
        "Phase 0 file-list hash differs from the Phase 1 binding",
    )

    provider = OllamaProvider(
        base_url=arguments.base_url,
        model=arguments.model,
        request_timeout_seconds=arguments.request_timeout,
        max_retries=arguments.max_retries,
        retry_backoff_seconds=arguments.retry_backoff_seconds,
    )
    version_response = provider.version(timeout_seconds=arguments.health_timeout)
    version = version_response.get("version")
    require(isinstance(version, str) and version.strip(), "Ollama version is missing")

    tags_response = provider.tags(timeout_seconds=arguments.model_timeout)
    installed = selected_model(tags_response, arguments.model)
    require(
        DIGEST_RE.fullmatch(arguments.expected_digest or "") is not None,
        "--expected-digest must be a full 64-hex SHA-256",
    )
    require(
        installed["digest"] == arguments.expected_digest,
        "Installed model digest differs from --expected-digest; refusing to replace the lock",
    )
    show_response = provider.show(verbose=False)
    verbose_show = provider.show(verbose=True)
    provider.bind_model_digest(installed["digest"])
    tokenizer = tokenizer_fingerprint(verbose_show)
    semantic = build_semantic(
        arguments=arguments,
        ollama_version=version,
        installed=installed,
        show=show_response,
        tokenizer=tokenizer,
    )
    semantic_sha256, experiment_id = semantic_identity(semantic)
    options = semantic["runtime_profile"]["options"]

    smoke_responses: list[dict[str, Any]] = []
    parsed_smoke: list[dict[str, Any]] = []
    for _ in range(2):
        response = provider.chat(
            messages=SMOKE_MESSAGES,
            response_schema=SMOKE_SCHEMA,
            options=options,
            stream=False,
            keep_alive=arguments.keep_alive,
        )
        smoke_responses.append(response)
        parsed_smoke.append(strict_smoke_content(response, arguments.model))
    require(canonical_json(parsed_smoke[0]) == canonical_json(parsed_smoke[1]),
            "Repeated structured smoke calls differ with the locked seed")

    envelope_summary = context_probe(
        provider, options, arguments.keep_alive
    )
    running_response = provider.running_models(timeout_seconds=arguments.model_timeout)
    running_receipt = running_model_receipt(
        running_response,
        arguments.model,
        installed["digest"],
        arguments.num_ctx,
    )

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    template = show_response["template"]
    parameters = show_response.get("parameters")
    artifacts: dict[str, Any] = {
        "ollama_version": {
            "captured_at_utc": generated_at,
            "response": version_response,
        },
        "model_tags": {
            "captured_at_utc": generated_at,
            "selected_model": installed,
        },
        "model_show": {
            "captured_at_utc": generated_at,
            "details": show_response.get("details"),
            "capabilities": show_response.get("capabilities"),
            "model_info": show_response.get("model_info"),
            "template": template,
            "template_sha256": sha256_bytes(template.encode("utf-8")),
            "template_length_characters": len(template),
            "parameters": parameters,
            "parameters_sha256": sha256_bytes((parameters or "").encode("utf-8")),
            "parameters_empty": not bool(parameters),
            "license_sha256": sha256_bytes(
                str(show_response.get("license", "")).encode("utf-8")
            ),
            "tokenizer": tokenizer,
        },
        "structured_output_smoke": {
            "captured_at_utc": generated_at,
            "status": "PASS",
            "request": {
                "model": arguments.model,
                "messages": SMOKE_MESSAGES,
                "format": SMOKE_SCHEMA,
                "options": options,
                "stream": False,
                "keep_alive": arguments.keep_alive,
            },
            "responses": smoke_responses,
            "parsed_outputs": parsed_smoke,
            "same_seed_canonical_output_equal": True,
        },
        "context_envelope": {
            "captured_at_utc": generated_at,
            "status": "PASS",
            "hard_limit_tokens": arguments.num_ctx,
            "component_caps": CONTEXT_COMPONENT_CAPS,
            "component_cap_sum": sum(CONTEXT_COMPONENT_CAPS.values()),
            "probe": envelope_summary,
        },
        "runtime_ps": {
            "captured_at_utc": generated_at,
            "status": "PASS",
            "loaded_model": running_receipt,
        },
    }
    artifact_paths = {
        "ollama_version": "artifacts/preflight/ollama_version.json",
        "model_tags": "artifacts/preflight/model_tags.json",
        "model_show": "artifacts/preflight/model_show.json",
        "structured_output_smoke": "artifacts/preflight/structured_output_smoke.json",
        "context_envelope": "artifacts/preflight/context_envelope.json",
        "runtime_ps": "artifacts/preflight/runtime_ps.json",
    }
    for name, relative_path in artifact_paths.items():
        write_json(root / relative_path, artifacts[name])

    entries = [
        artifact_entry(root, name, relative_path)
        for name, relative_path in sorted(artifact_paths.items())
    ]
    report = {
        "phase": 1,
        "status": "PASS",
        "generated_at_utc": generated_at,
        "experiment_id": experiment_id,
        "semantic_sha256": semantic_sha256,
        "checks": {
            "ollama_health": "PASS",
            "model_presence": "PASS",
            "full_manifest_digest": "PASS",
            "model_metadata": "PASS",
            "structured_json_chat_twice": "PASS",
            "same_seed_canonical_output": "PASS",
            "effective_num_ctx": "PASS",
            "context_envelope_high_water": "PASS",
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "ollama_processor": running_receipt["processor"],
        },
        "artifacts": entries,
    }
    report_path = "artifacts/preflight/preflight_report.json"
    write_json(root / report_path, report)
    entries.append(artifact_entry(root, "preflight_report", report_path))

    lock = {
        "schema_version": 1,
        "identity": {
            "experiment_id": experiment_id,
            "semantic_sha256": semantic_sha256,
            "hash_domain": "ai-sast-experiment-semantic-v1",
            "canonicalization": "utf8_sorted_keys_compact_json_allow_nan_false",
        },
        "semantic": semantic,
        "evidence": {
            "captured_at_utc": generated_at,
            "preflight_status": "PASS",
            "artifacts": entries,
        },
        "invalidation": {
            "trigger": "any_semantic_experiment_lock_change",
            "action": "issue_new_experiment_id_and_rebuild_downstream_artifacts",
            "volatile_evidence_changes_experiment_id": False,
            "phase2_evaluation_binding_changes_experiment_id": True,
        },
    }
    write_json(root / "experiment.lock.yaml", lock)
    return lock


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    value.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    value.add_argument("--model", default=os.environ.get("OLLAMA_MODEL"))
    value.add_argument(
        "--expected-digest",
        default=os.environ.get("OLLAMA_MODEL_DIGEST"),
    )
    value.add_argument("--num-ctx", type=int, default=16384)
    value.add_argument("--num-predict", type=int, default=1536)
    value.add_argument("--temperature", type=float, default=0.0)
    value.add_argument("--seed", type=int, default=20260811)
    value.add_argument("--top-k", type=int, default=20)
    value.add_argument("--top-p", type=float, default=0.8)
    value.add_argument("--min-p", type=float, default=0.0)
    value.add_argument("--repeat-last-n", type=int, default=64)
    value.add_argument("--repeat-penalty", type=float, default=1.05)
    value.add_argument("--keep-alive", default="5m")
    value.add_argument("--max-retries", type=int, default=2)
    value.add_argument("--retry-backoff-seconds", type=float, nargs="+", default=[1.0, 2.0])
    value.add_argument("--health-timeout", type=float, default=10.0)
    value.add_argument("--model-timeout", type=float, default=30.0)
    value.add_argument("--request-timeout", type=float, default=600.0)
    return value


def main() -> int:
    arguments = parser().parse_args()
    if not arguments.model or not arguments.expected_digest:
        print(
            "ERROR --model/OLLAMA_MODEL and --expected-digest/OLLAMA_MODEL_DIGEST are required",
            file=sys.stderr,
        )
        return 2
    try:
        lock = run(arguments)
    except (PreflightError, ProviderError, OSError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print("PASS Phase 1 Ollama preflight and experiment lock")
    print(f"experiment_id={lock['identity']['experiment_id']}")
    print(f"semantic_sha256={lock['identity']['semantic_sha256']}")
    print(f"model_digest={lock['semantic']['model']['manifest_digest']}")
    print(f"num_ctx={lock['semantic']['runtime_profile']['options']['num_ctx']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
