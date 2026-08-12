#!/usr/bin/env python3
"""Run exactly the three evaluation Batches frozen in Phase 2."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime import SelectedBatchRunner  # noqa: E402


def find_git(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.resolve()
        if not candidate.is_file():
            raise ValueError(f"Git executable not found: {candidate}")
        return candidate
    discovered = shutil.which("git")
    if discovered:
        return Path(discovered).resolve()
    raise ValueError("Git executable not found on PATH")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="AI-SAST project root",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(".target-src/userland"),
        help="Locked Raspberry Pi Userland checkout",
    )
    parser.add_argument("--git", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reconstruct and verify the frozen three Batch payloads without Ollama",
    )
    args = parser.parse_args()

    try:
        root = args.root.resolve()
        repository = (
            args.repo if args.repo.is_absolute() else root / args.repo
        ).resolve()
        git_executable = find_git(args.git)
        runner = SelectedBatchRunner(
            root=root,
            repository=repository,
            git_executable=git_executable,
        )
        output = runner.dry_run() if args.dry_run else runner.run()
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            f"Selected Batch runtime failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
