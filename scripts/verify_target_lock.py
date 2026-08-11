#!/usr/bin/env python3
"""Independent, read-only verifier for the Phase 0 target contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "https://github.com/raspberrypi/userland.git"
REPOSITORY_WEB_URL = "https://github.com/raspberrypi/userland"
COMMIT_SHA = "a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976"
COMMIT_DATE = "2024-12-23"
INCLUDE_PATTERNS = ["**/*.c", "**/*.h", "**/*.cpp"]
INCLUDE_EXTENSIONS = {".c", ".h", ".cpp"}
PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|TBC|FIXME|CHANGEME|REPLACE_ME|PLACEHOLDER)\b"
    r"|\$\{|\{\{|<[^>]+>|example\.com",
    re.IGNORECASE,
)


class ContractError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ContractError(message)


def git_executable(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            fail(f"Git executable does not exist: {path}")
        return str(path)
    discovered = shutil.which("git")
    if discovered:
        return discovered
    windows_git = Path(r"C:\Program Files\Git\cmd\git.exe")
    if windows_git.is_file():
        return str(windows_git)
    fail("Git executable was not found; pass --git with an explicit path")


def run_git(git: str, repo: Path, *arguments: str) -> bytes:
    command = [
        git,
        "-c",
        f"safe.directory={repo.as_posix()}",
        "-c",
        "core.quotepath=false",
        "-C",
        str(repo),
        *arguments,
    ]
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        fail(f"Git command failed ({process.returncode}): {' '.join(arguments)}\n{detail}")
    return process.stdout


def read_utf8_no_bom_lf(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail(f"UTF-8 BOM is not allowed: {path}")
    if b"\r" in raw:
        fail(f"CR/CRLF line endings are not allowed: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        fail(f"File is not valid UTF-8: {path}: {error}")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> Any:
    text = read_utf8_no_bom_lf(path)
    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as error:
        fail(f"Invalid JSON in {path}: {error}")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    text = read_utf8_no_bom_lf(path)
    if not text.endswith("\n"):
        fail(f"JSONL must end with LF: {path}")
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            fail(f"Blank JSONL record at {path}:{line_number}")
        try:
            item = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as error:
            fail(f"Invalid JSONL record at {path}:{line_number}: {error}")
        if not isinstance(item, dict):
            fail(f"JSONL record must be an object at {path}:{line_number}")
        result.append(item)
    return result


def assert_no_placeholders(value: Any, location: str = "$") -> None:
    if isinstance(value, str):
        if not value.strip():
            fail(f"Empty string is not allowed at {location}")
        match = PLACEHOLDER_RE.search(value)
        if match:
            fail(f"Placeholder-like value at {location}: {match.group(0)}")
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_no_placeholders(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_placeholders(item, f"{location}[{index}]")


def json_type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(expected, True)


def validate_schema(instance: Any, schema: dict[str, Any], location: str = "$") -> None:
    if "const" in schema and instance != schema["const"]:
        fail(f"Schema const violation at {location}")
    if "enum" in schema and instance not in schema["enum"]:
        fail(f"Schema enum violation at {location}")
    expected_type = schema.get("type")
    if expected_type and not json_type_matches(instance, expected_type):
        fail(f"Schema type violation at {location}: expected {expected_type}")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in instance]
        if missing:
            fail(f"Schema required-property violation at {location}: {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                fail(f"Schema additional-property violation at {location}: {extras}")
        for key, item in instance.items():
            if key in properties:
                validate_schema(item, properties[key], f"{location}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(item, schema["additionalProperties"], f"{location}.{key}")

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            fail(f"Schema minItems violation at {location}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            fail(f"Schema maxItems violation at {location}")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in instance]
            if len(canonical) != len(set(canonical)):
                fail(f"Schema uniqueItems violation at {location}")
        prefix_items = schema.get("prefixItems", [])
        for index, item_schema in enumerate(prefix_items[: len(instance)]):
            validate_schema(instance[index], item_schema, f"{location}[{index}]")
        item_schema = schema.get("items")
        if item_schema is False and len(instance) > len(prefix_items):
            fail(f"Schema additional array item violation at {location}")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance[len(prefix_items) :], start=len(prefix_items)):
                validate_schema(item, item_schema, f"{location}[{index}]")

    if isinstance(instance, str):
        if "pattern" in schema and not re.fullmatch(schema["pattern"], instance):
            fail(f"Schema pattern violation at {location}")
        if schema.get("format") == "date":
            try:
                date.fromisoformat(instance)
            except ValueError:
                fail(f"Schema date violation at {location}")

    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            fail(f"Schema minimum violation at {location}")


def out_of_scope_reason(mode: str, extension: str) -> str:
    if mode == "120000":
        return "symbolic link; links are not followed in the v1 target contract"
    return {
        ".s": "ARM assembly; outside the v1 C/C++ parser scope",
        ".qasm": "VideoCore QPU assembly; outside the v1 C/C++ parser scope",
        ".qinc": "VideoCore QPU assembly include; outside the v1 C/C++ parser scope",
        ".in": "build/config template; not a concrete C/C++ parser input",
    }.get(extension, "tracked non-C/C++ artifact")


def dotnet_path_extension(path: str) -> str:
    """Match System.IO.Path.GetExtension for the fixed POSIX Git paths."""
    name = path.rsplit("/", 1)[-1]
    separator = name.rfind(".")
    return "" if separator < 0 else name[separator:]


def inventory_from_git(git: str, repo: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw = run_git(git, repo, "ls-tree", "-r", "-z", "-l", "--full-tree", COMMIT_SHA)
    pattern = re.compile(rb"^(\d{6})\s+(\w+)\s+([0-9a-f]+)\s+(-|\d+)\t(.*)$", re.DOTALL)
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_record in raw.split(b"\0"):
        if not raw_record:
            continue
        match = pattern.fullmatch(raw_record)
        if not match:
            fail(f"Unsupported NUL-delimited ls-tree record: {raw_record[:120]!r}")
        mode, object_type, oid, size, raw_path = match.groups()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            fail(f"Target path is not UTF-8: {error}")
        if path in seen:
            fail(f"Duplicate tracked path: {path}")
        seen.add(path)
        mode_text = mode.decode("ascii")
        type_text = object_type.decode("ascii")
        extension = dotnet_path_extension(path)
        regular_blob = type_text == "blob" and mode_text in {"100644", "100755"}
        in_scope = regular_blob and extension in INCLUDE_EXTENSIONS
        entry = {
            "path": path,
            "git_blob_oid": oid.decode("ascii"),
            "mode": mode_text,
            "size_bytes": None if size == b"-" else int(size),
            "extension": extension,
            "classification": "IN_SCOPE" if in_scope else "OUT_OF_SCOPE",
            "reason": (
                "tracked C/C++ source or header; no path exclusion applies"
                if in_scope
                else out_of_scope_reason(mode_text, extension)
            ),
        }
        inventory.append(entry)

    inventory.sort(key=lambda item: item["path"].encode("utf-8"))
    source = [item for item in inventory if item["classification"] == "IN_SCOPE"]
    excluded = [item for item in inventory if item["classification"] == "OUT_OF_SCOPE"]
    return inventory, source, excluded


def file_list_hash(source: list[dict[str, Any]]) -> str:
    payload = b"ai-sast-target-file-list-v1\0" + b"".join(
        item["path"].encode("utf-8") + b"\0" for item in source
    )
    return hashlib.sha256(payload).hexdigest()


def scope_hash(file_hash: str, file_count: int) -> tuple[str, str]:
    semantic = {
        "allow_submodules": False,
        "analysis_content_source": "git_blob_at_locked_commit",
        "build_reachability_filter": "disabled",
        "bundled_opensource": "include",
        "commit_sha": COMMIT_SHA,
        "exclude": [],
        "examples": "include",
        "file_count": file_count,
        "follow_symlinks": False,
        "include": sorted(INCLUDE_PATTERNS),
        "language_family": ["c", "cpp"],
        "path_case_sensitive": True,
        "precedence": "exclude_wins",
        "preprocessor_branch_filter": "disabled",
        "repository_url": REPOSITORY_URL,
        "require_clean_worktree": True,
        "require_exact_commit": True,
        "schema_version": 1,
        "source": "git_tree",
        "target_file_list_sha256": file_hash,
        "tests": "include",
        "tracked_only": True,
    }
    canonical = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return (
        hashlib.sha256(b"ai-sast-scope-v1\0" + canonical).hexdigest(),
        canonical.decode("utf-8"),
    )


def verify(repo: Path, git: str) -> None:
    if not repo.is_dir():
        fail(f"Target checkout does not exist: {repo}")
    origin = run_git(git, repo, "remote", "get-url", "origin").decode().strip()
    head = run_git(git, repo, "rev-parse", "HEAD").decode().strip()
    commit_type = run_git(git, repo, "cat-file", "-t", COMMIT_SHA).decode().strip()
    commit_date = run_git(git, repo, "show", "-s", "--format=%cs", COMMIT_SHA).decode().strip()
    status = run_git(git, repo, "status", "--porcelain=v1", "--untracked-files=all")
    if origin != REPOSITORY_URL:
        fail(f"Origin mismatch: {origin}")
    if head != COMMIT_SHA or commit_type != "commit" or commit_date != COMMIT_DATE:
        fail("Locked commit identity mismatch")
    if status:
        fail("Target worktree is not clean")

    inventory, source, excluded = inventory_from_git(git, repo)
    if (len(inventory), len(source), len(excluded)) != (830, 654, 176):
        fail(f"Inventory mismatch: {(len(inventory), len(source), len(excluded))}")
    if len({item["path"] for item in inventory}) != 830:
        fail("Duplicate inventory path")
    extension_counts = Counter(item["extension"] for item in source)
    if extension_counts != Counter({".c": 284, ".h": 367, ".cpp": 3}):
        fail(f"Extension inventory mismatch: {extension_counts}")
    top_counts = Counter(item["path"].split("/", 1)[0] for item in source)
    expected_top = {
        "containers": 104, "helpers": 8, "host_applications": 127,
        "host_support": 2, "interface": 371, "makefiles": 1,
        "middleware": 20, "opensrc": 13, "vcfw": 5, "vcinclude": 3,
    }
    if dict(sorted(top_counts.items())) != expected_top:
        fail(f"Top-level inventory mismatch: {top_counts}")

    file_hash = file_list_hash(source)
    semantic_hash, scope_canonical_json = scope_hash(file_hash, len(source))
    lock_path = PROJECT_ROOT / "target.lock.yaml"
    schema_path = PROJECT_ROOT / "schemas" / "target-lock.schema.json"
    profile_path = PROJECT_ROOT / "docs" / "target_profile.md"
    lock = load_json(lock_path)
    schema = load_json(schema_path)
    assert_no_placeholders(lock)
    assert_no_placeholders(schema)
    profile = read_utf8_no_bom_lf(profile_path)
    if PLACEHOLDER_RE.search(profile):
        fail("Placeholder-like value in target profile")
    validate_schema(lock, schema)

    if lock["target"]["commit_sha"] != COMMIT_SHA:
        fail("Lock commit mismatch")
    if lock["checkout"]["analysis_content_source"] != "git_blob_at_locked_commit":
        fail("Analysis content must come from the locked Git blob")
    if lock["analysis_scope"]["target_file_list_sha256"] != file_hash:
        fail("Target file-list hash mismatch")
    if lock["analysis_scope"]["scope_sha256"] != semantic_hash:
        fail(
            "Scope hash mismatch: "
            f"lock={lock['analysis_scope']['scope_sha256']} independent={semantic_hash} "
            f"canonical={scope_canonical_json!r}"
        )

    inventory_document = load_json(PROJECT_ROOT / lock["artifacts"]["target_inventory"])
    source_manifest = load_jsonl(PROJECT_ROOT / lock["artifacts"]["source_manifest"])
    exclusion_manifest = load_jsonl(PROJECT_ROOT / lock["artifacts"]["exclusion_manifest"])
    verification = load_json(PROJECT_ROOT / lock["artifacts"]["target_verification"])
    if inventory_document["files"] != inventory:
        generated_files = inventory_document["files"]
        for index, (generated, independent) in enumerate(zip(generated_files, inventory)):
            if generated != independent:
                fail(
                    "Target inventory differs from independent Git-tree reconstruction "
                    f"at index {index}: generated={generated!r} independent={independent!r}"
                )
        fail(
            "Target inventory length differs from independent Git-tree reconstruction: "
            f"generated={len(generated_files)} independent={len(inventory)}"
        )
    if source_manifest != source:
        fail("Source manifest differs from independent Git-tree reconstruction")
    if exclusion_manifest != excluded:
        fail("Exclusion manifest differs from independent Git-tree reconstruction")
    if verification["status"] != "PASS" or verification["scope_sha256"] != semantic_hash:
        fail("Target verification receipt mismatch")
    if verification.get("scope_hash_canonical_json") != scope_canonical_json:
        fail(
            "Canonical scope hash input mismatch: "
            f"generator={verification.get('scope_hash_canonical_json')!r} "
            f"independent={scope_canonical_json!r}"
        )

    for required in (REPOSITORY_URL, COMMIT_SHA, file_hash, semantic_hash):
        if required not in profile:
            fail(f"Target profile is missing required value: {required}")

    print("PASS independent Phase 0 target contract verification")
    print(f"commit_sha={COMMIT_SHA}")
    print(f"in_scope_files={len(source)}")
    print(f"target_file_list_sha256={file_hash}")
    print(f"scope_sha256={semantic_hash}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT / ".target-src" / "userland")
    parser.add_argument("--git", dest="git_path")
    arguments = parser.parse_args()
    try:
        verify(arguments.repo.resolve(), git_executable(arguments.git_path))
    except (ContractError, OSError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
