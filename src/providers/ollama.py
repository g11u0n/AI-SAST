"""Strict standard-library Ollama HTTP provider for the v1 runtime."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib import error, parse, request

from .base import LLMProvider, ProviderError


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError(f"Ollama returned a duplicate JSON key: {key}")
        result[key] = value
    return result


class OllamaProvider(LLMProvider):
    """Ollama API client with bounded retry and strict JSON parsing."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        request_timeout_seconds: float,
        max_retries: int,
        retry_backoff_seconds: Sequence[float],
        expected_model_digest: str | None = None,
        locked_options: Mapping[str, Any] | None = None,
        locked_keep_alive: str | None = None,
        locked_context_envelope: Mapping[str, Any] | None = None,
        allow_remote_endpoint: bool = False,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        parsed = parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid Ollama base URL: {base_url!r}")
        if parsed.query or parsed.fragment:
            raise ValueError("Ollama base URL must not contain a query or fragment")
        if not model.strip():
            raise ValueError("Ollama model must not be empty")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if len(retry_backoff_seconds) != max_retries:
            raise ValueError("retry_backoff_seconds must contain one value per retry")
        if any(value < 0 for value in retry_backoff_seconds):
            raise ValueError("retry backoff values must be non-negative")
        if expected_model_digest is not None and re.fullmatch(
            r"[0-9a-f]{64}", expected_model_digest
        ) is None:
            raise ValueError("expected_model_digest must be a full 64-hex SHA-256")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and not allow_remote_endpoint:
            raise ValueError(
                "Non-loopback Ollama endpoints require allow_remote_endpoint=true"
            )
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = tuple(float(value) for value in retry_backoff_seconds)
        self.expected_model_digest = expected_model_digest
        self.locked_options = dict(locked_options) if locked_options is not None else None
        self.locked_keep_alive = locked_keep_alive
        self.locked_context_envelope = (
            dict(locked_context_envelope)
            if locked_context_envelope is not None
            else None
        )
        self.max_response_bytes = max_response_bytes

    @classmethod
    def from_experiment_lock(cls, lock: Mapping[str, Any]) -> "OllamaProvider":
        """Create a fail-closed runtime provider from the sealed semantic profile."""
        semantic = lock["semantic"]
        canonical = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(
            b"ai-sast-experiment-semantic-v1\0" + canonical
        ).hexdigest()
        identity = lock["identity"]
        if identity.get("semantic_sha256") != digest:
            raise ProviderError("Experiment lock semantic SHA-256 is invalid")
        if identity.get("experiment_id") != "exp-v1-" + digest[:24]:
            raise ProviderError("Experiment lock ID is invalid")
        backend = semantic["backend"]
        if backend["provider"] != "ollama":
            raise ValueError("The v1 experiment lock must use Ollama")
        profile = semantic["runtime_profile"]
        retry = profile["retry"]
        return cls(
            base_url=backend["base_url"],
            model=semantic["model"]["name"],
            request_timeout_seconds=profile["timeouts_seconds"]["request"],
            max_retries=retry["max_retries"],
            retry_backoff_seconds=retry["backoff_seconds"],
            expected_model_digest=semantic["model"]["manifest_digest"],
            locked_options=profile["options"],
            locked_keep_alive=profile["transport"]["keep_alive"],
            locked_context_envelope=semantic["context_envelope"],
            allow_remote_endpoint=not profile["transport"]["loopback_only"],
            max_response_bytes=profile["transport"]["max_response_bytes"],
        )

    def _request_json(
        self,
        path: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None = None,
        retry: bool = True,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Ollama API path must start with '/'")
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempts = self.max_retries + 1 if retry else 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            api_request = request.Request(
                self.base_url + path,
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with request.urlopen(
                    api_request,
                    timeout=timeout_seconds or self.request_timeout_seconds,
                ) as response:
                    raw = response.read(self.max_response_bytes + 1)
                    if len(raw) > self.max_response_bytes:
                        raise ProviderError(
                            f"Ollama {path} response exceeds {self.max_response_bytes} bytes"
                        )
                    if response.status < 200 or response.status >= 300:
                        raise ProviderError(
                            f"Ollama {path} returned HTTP {response.status}"
                        )
                try:
                    decoded = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProviderError(
                        f"Ollama {path} returned non-UTF-8 JSON"
                    ) from exc
                try:
                    value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"Ollama {path} returned invalid JSON") from exc
                if not isinstance(value, dict):
                    raise ProviderError(f"Ollama {path} response must be a JSON object")
                return value
            except error.HTTPError as exc:
                detail = exc.read(512).decode("utf-8", errors="replace").strip()
                last_error = ProviderError(
                    f"Ollama {path} returned HTTP {exc.code}: {detail}"
                )
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable:
                    raise last_error from exc
            except (error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                last_error = exc
            except ProviderError:
                raise

            if attempt + 1 < attempts:
                time.sleep(self.retry_backoff_seconds[attempt])

        raise ProviderError(
            f"Ollama {path} failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    def version(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        return self._request_json(
            "/api/version",
            method="GET",
            retry=False,
            timeout_seconds=timeout_seconds,
        )

    def tags(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        return self._request_json(
            "/api/tags",
            method="GET",
            timeout_seconds=timeout_seconds,
        )

    def bind_model_digest(self, digest: str) -> None:
        """Bind an immutable digest once discovery has resolved the model tag."""
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Model digest must be a full 64-hex SHA-256")
        if self.expected_model_digest is not None and self.expected_model_digest != digest:
            raise ProviderError("Cannot replace an already-bound model digest")
        self.expected_model_digest = digest

    def assert_model_identity(self) -> None:
        """Fail closed if the configured tag no longer resolves to the locked digest."""
        if self.expected_model_digest is None:
            raise ProviderError("No immutable model digest is bound to this provider")
        response = self.tags()
        models = response.get("models")
        if not isinstance(models, list):
            raise ProviderError("Ollama /api/tags response has no models array")
        matches = [
            item
            for item in models
            if isinstance(item, dict) and item.get("name") == self.model
        ]
        if len(matches) != 1:
            raise ProviderError(
                f"Expected exactly one installed model named {self.model!r}"
            )
        if matches[0].get("digest") != self.expected_model_digest:
            raise ProviderError(
                "Ollama model tag no longer resolves to the locked manifest digest"
            )

    def show(self, *, verbose: bool = False) -> dict[str, Any]:
        return self._request_json(
            "/api/show",
            method="POST",
            payload={"model": self.model, "verbose": verbose},
        )

    def running_models(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        return self._request_json(
            "/api/ps",
            method="GET",
            timeout_seconds=timeout_seconds,
        )

    def chat(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
        options: Mapping[str, Any],
        stream: bool,
        keep_alive: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
        component_payloads: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if stream:
            raise ValueError("The v1 provider accepts only stream=false")
        if not messages:
            raise ValueError("At least one chat message is required")
        self.assert_model_identity()
        if self.locked_options is not None:
            self._validate_locked_request(
                messages=messages,
                response_schema=response_schema,
                options=options,
                stream=stream,
                keep_alive=keep_alive,
                tools=tools,
                component_payloads=component_payloads,
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "options": dict(options),
            "stream": False,
            "keep_alive": keep_alive,
        }
        if response_schema is not None:
            payload["format"] = dict(response_schema)
        if tools is not None:
            payload["tools"] = [dict(tool) for tool in tools]
        response = self._request_json("/api/chat", method="POST", payload=payload)
        if self.locked_options is not None:
            self._validate_locked_response(response, response_schema)
        return response

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def _validate_locked_request(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
        options: Mapping[str, Any],
        stream: bool,
        keep_alive: str,
        tools: Sequence[Mapping[str, Any]] | None,
        component_payloads: Mapping[str, str] | None,
    ) -> None:
        if dict(options) != self.locked_options:
            raise ProviderError("Request options differ from the experiment lock")
        if stream:
            raise ProviderError("Locked v1 requests require stream=false")
        if keep_alive != self.locked_keep_alive:
            raise ProviderError("Request keep_alive differs from the experiment lock")
        if response_schema is None:
            raise ProviderError("Locked v1 requests require a response JSON Schema")
        self._validate_response_schema_contract(response_schema, "$")
        if component_payloads is None:
            raise ProviderError("Locked v1 requests require named context components")
        expected_payload_keys = {
            "system_instruction",
            "user_instruction",
            "evidence_or_raw_batch",
            "structured_state",
        }
        if set(component_payloads) != expected_payload_keys:
            raise ProviderError("Context component payload set differs from the lock")
        if any(not isinstance(value, str) for value in component_payloads.values()):
            raise ProviderError("Every context component payload must be text")

        system_messages = [
            item.get("content")
            for item in messages
            if item.get("role") == "system"
        ]
        user_messages = [
            item.get("content")
            for item in messages
            if item.get("role") == "user"
        ]
        if len(system_messages) != 1 or system_messages[0] != component_payloads["system_instruction"]:
            raise ProviderError("System message is not bound to the system component")
        if len(user_messages) != 1 or not isinstance(user_messages[0], str):
            raise ProviderError("Locked v1 requests require exactly one text user message")
        cursor = 0
        for name in ("user_instruction", "evidence_or_raw_batch", "structured_state"):
            component = component_payloads[name]
            position = user_messages[0].find(component, cursor)
            if position < 0:
                raise ProviderError(f"User message does not contain ordered component: {name}")
            cursor = position + len(component)

        envelope = self.locked_context_envelope
        if not isinstance(envelope, dict):
            raise ProviderError("Locked provider has no context envelope")
        caps = envelope["component_caps"]
        byte_upper_bounds = {
            "system_instruction_tokens": len(
                component_payloads["system_instruction"].encode("utf-8")
            ),
            "user_instruction_tokens": len(
                component_payloads["user_instruction"].encode("utf-8")
            ),
            "evidence_or_raw_batch_tokens": len(
                component_payloads["evidence_or_raw_batch"].encode("utf-8")
            ),
            "structured_state_tokens": len(
                component_payloads["structured_state"].encode("utf-8")
            ),
            "tool_and_response_schema_tokens": len(
                self._canonical_bytes(
                    {
                        "response_schema": dict(response_schema),
                        "tools": [dict(item) for item in tools] if tools is not None else [],
                    }
                )
            ),
        }
        for name, count in byte_upper_bounds.items():
            if count > caps[name]:
                raise ProviderError(
                    f"Context component exceeds conservative cap: {name} {count} > {caps[name]}"
                )
        message_and_schema_bytes = len(
            self._canonical_bytes(
                {
                    "messages": [dict(item) for item in messages],
                    "response_schema": dict(response_schema),
                    "tools": [dict(item) for item in tools] if tools is not None else [],
                }
            )
        )
        conservative_total = (
            message_and_schema_bytes + caps["chat_template_overhead_tokens"]
        )
        if conservative_total > envelope["input_cap_tokens"]:
            raise ProviderError(
                "Conservative request bound exceeds the locked input context cap"
            )

    def _validate_locked_response(
        self,
        response: Mapping[str, Any],
        response_schema: Mapping[str, Any] | None = None,
    ) -> None:
        if response.get("model") != self.model:
            raise ProviderError("Ollama response model differs from the experiment lock")
        if response.get("done") is not True or response.get("done_reason") != "stop":
            raise ProviderError("Ollama response must complete with done_reason=stop")
        prompt_count = response.get("prompt_eval_count")
        output_count = response.get("eval_count")
        if not isinstance(prompt_count, int) or isinstance(prompt_count, bool) or prompt_count <= 0:
            raise ProviderError("Ollama response has no authoritative input counter")
        if not isinstance(output_count, int) or isinstance(output_count, bool) or output_count <= 0:
            raise ProviderError("Ollama response has no authoritative output counter")
        envelope = self.locked_context_envelope
        if prompt_count > envelope["input_cap_tokens"]:
            raise ProviderError("Provider input counter exceeded the locked input cap")
        if output_count > envelope["component_caps"]["reserved_output_tokens"]:
            raise ProviderError("Provider output counter exceeded the locked output reservation")
        message = response.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
        ):
            raise ProviderError("Structured Ollama response has no text content")
        try:
            parsed = json.loads(
                message["content"], object_pairs_hook=_reject_duplicate_keys
            )
        except json.JSONDecodeError as exc:
            raise ProviderError("Structured Ollama response content is invalid JSON") from exc
        if response_schema is not None:
            self._validate_schema_value(parsed, response_schema, "$")

    @classmethod
    def _validate_response_schema_contract(
        cls,
        schema: Mapping[str, Any],
        location: str,
    ) -> None:
        supported = {
            "type", "properties", "required", "additionalProperties", "const", "enum",
            "items", "minItems", "maxItems", "minimum", "maximum", "minLength",
            "maxLength", "pattern", "title", "description", "$schema",
        }
        extras = sorted(set(schema) - supported)
        if extras:
            raise ProviderError(
                f"Unsupported response-schema keywords at {location}: {extras}"
            )
        expected_type = schema.get("type")
        if expected_type not in {
            "object", "array", "string", "integer", "number", "boolean", "null"
        }:
            raise ProviderError(
                f"Response schema requires one supported type at {location}"
            )
        if "enum" in schema and (
            not isinstance(schema["enum"], list) or not schema["enum"]
        ):
            raise ProviderError(f"Response schema enum must be non-empty at {location}")

        if expected_type == "object":
            properties = schema.get("properties")
            required = schema.get("required")
            if not isinstance(properties, Mapping):
                raise ProviderError(
                    f"Object response schema requires properties at {location}"
                )
            if not isinstance(required, list) or any(
                not isinstance(key, str) for key in required
            ):
                raise ProviderError(
                    f"Object response schema requires a string required-list at {location}"
                )
            if len(required) != len(set(required)) or not set(required).issubset(properties):
                raise ProviderError(
                    f"Object response schema required-list is invalid at {location}"
                )
            if schema.get("additionalProperties") is not False:
                raise ProviderError(
                    f"Object response schema must reject additional properties at {location}"
                )
            for key, child in properties.items():
                if not isinstance(key, str) or not isinstance(child, Mapping):
                    raise ProviderError(
                        f"Invalid object response-schema property at {location}"
                    )
                cls._validate_response_schema_contract(child, f"{location}.{key}")
        elif any(key in schema for key in ("properties", "required", "additionalProperties")):
            raise ProviderError(
                f"Object-only schema keyword used by {expected_type} at {location}"
            )

        if expected_type == "array":
            items = schema.get("items")
            if not isinstance(items, Mapping):
                raise ProviderError(
                    f"Array response schema requires an items schema at {location}"
                )
            cls._validate_response_schema_contract(items, f"{location}[]")
            for key in ("minItems", "maxItems"):
                if key in schema and (
                    not isinstance(schema[key], int)
                    or isinstance(schema[key], bool)
                    or schema[key] < 0
                ):
                    raise ProviderError(f"Invalid {key} at {location}")
            if schema.get("minItems", 0) > schema.get("maxItems", float("inf")):
                raise ProviderError(f"minItems exceeds maxItems at {location}")
        elif any(key in schema for key in ("items", "minItems", "maxItems")):
            raise ProviderError(
                f"Array-only schema keyword used by {expected_type} at {location}"
            )

        if expected_type in {"integer", "number"}:
            for key in ("minimum", "maximum"):
                if key in schema and (
                    not isinstance(schema[key], (int, float))
                    or isinstance(schema[key], bool)
                ):
                    raise ProviderError(f"Invalid {key} at {location}")
            if schema.get("minimum", float("-inf")) > schema.get("maximum", float("inf")):
                raise ProviderError(f"minimum exceeds maximum at {location}")
        elif any(key in schema for key in ("minimum", "maximum")):
            raise ProviderError(
                f"Numeric-only schema keyword used by {expected_type} at {location}"
            )

        if expected_type == "string":
            for key in ("minLength", "maxLength"):
                if key in schema and (
                    not isinstance(schema[key], int)
                    or isinstance(schema[key], bool)
                    or schema[key] < 0
                ):
                    raise ProviderError(f"Invalid {key} at {location}")
            if schema.get("minLength", 0) > schema.get("maxLength", float("inf")):
                raise ProviderError(f"minLength exceeds maxLength at {location}")
            if "pattern" in schema:
                if not isinstance(schema["pattern"], str):
                    raise ProviderError(f"Invalid pattern at {location}")
                try:
                    re.compile(schema["pattern"])
                except re.error as exc:
                    raise ProviderError(
                        f"Invalid response-schema regex at {location}: {exc}"
                    ) from exc
        elif any(key in schema for key in ("minLength", "maxLength", "pattern")):
            raise ProviderError(
                f"String-only schema keyword used by {expected_type} at {location}"
            )

    @classmethod
    def _validate_schema_value(
        cls,
        value: Any,
        schema: Mapping[str, Any],
        location: str,
    ) -> None:
        expected_type = schema.get("type")
        matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if isinstance(expected_type, str) and not matches.get(expected_type, True):
            raise ProviderError(
                f"Structured response schema type violation at {location}"
            )
        if "const" in schema and value != schema["const"]:
            raise ProviderError(
                f"Structured response schema const violation at {location}"
            )
        if "enum" in schema and value not in schema["enum"]:
            raise ProviderError(
                f"Structured response schema enum violation at {location}"
            )
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            missing = [key for key in schema.get("required", []) if key not in value]
            if missing:
                raise ProviderError(
                    f"Structured response missing required fields at {location}: {missing}"
                )
            if schema.get("additionalProperties") is False:
                extras = sorted(set(value) - set(properties))
                if extras:
                    raise ProviderError(
                        f"Structured response has extra fields at {location}: {extras}"
                    )
            for key, item in value.items():
                child = properties.get(key)
                if isinstance(child, Mapping):
                    cls._validate_schema_value(item, child, f"{location}.{key}")
        if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
            if len(value) < schema.get("minItems", 0):
                raise ProviderError(
                    f"Structured response schema minItems violation at {location}"
                )
            if len(value) > schema.get("maxItems", float("inf")):
                raise ProviderError(
                    f"Structured response schema maxItems violation at {location}"
                )
            for index, item in enumerate(value):
                cls._validate_schema_value(
                    item, schema["items"], f"{location}[{index}]"
                )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < schema.get("minimum", float("-inf")):
                raise ProviderError(
                    f"Structured response schema minimum violation at {location}"
                )
            if value > schema.get("maximum", float("inf")):
                raise ProviderError(
                    f"Structured response schema maximum violation at {location}"
                )
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise ProviderError(
                    f"Structured response schema minLength violation at {location}"
                )
            if len(value) > schema.get("maxLength", float("inf")):
                raise ProviderError(
                    f"Structured response schema maxLength violation at {location}"
                )
            if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
                raise ProviderError(
                    f"Structured response schema pattern violation at {location}"
                )
