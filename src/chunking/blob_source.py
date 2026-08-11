"""Read immutable source bytes from Git objects, never from the worktree."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def git_blob_oid(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw).hexdigest()  # noqa: S324 - Git object identity


class GitBlobSource:
    """Persistent `git cat-file --batch` reader with independent object checks."""

    def __init__(self, *, git_executable: Path, repository: Path) -> None:
        self.git_executable = git_executable
        self.repository = repository
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "GitBlobSource":
        command = [
            str(self.git_executable),
            "-C",
            str(self.repository),
            "cat-file",
            "--batch",
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._process is None:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        if self._process.stdout is not None:
            self._process.stdout.close()
        if self._process.stderr is not None:
            self._process.stderr.close()
        self._process = None

    def read_blob(self, oid: str, *, expected_size: int) -> bytes:
        if len(oid) != 40 or any(char not in "0123456789abcdef" for char in oid):
            raise ValueError(f"Invalid full Git blob OID: {oid}")
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("GitBlobSource must be used as a context manager")
        process.stdin.write(oid.encode("ascii") + b"\n")
        process.stdin.flush()
        header = process.stdout.readline()
        if not header:
            stderr = b"" if process.stderr is None else process.stderr.read()
            raise ValueError(f"git cat-file terminated: {stderr.decode('utf-8', 'replace')}")
        fields = header.rstrip(b"\n").split(b" ")
        if len(fields) == 2 and fields[1] == b"missing":
            raise ValueError(f"Missing Git object: {oid}")
        if len(fields) != 3:
            raise ValueError(f"Unexpected git cat-file header for {oid}: {header!r}")
        actual_oid, object_type, size_text = fields
        if actual_oid.decode("ascii") != oid:
            raise ValueError(f"Git returned a different object for {oid}")
        if object_type != b"blob":
            raise ValueError(f"Git object is not a blob: {oid}")
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ValueError(f"Invalid Git object size for {oid}") from exc
        raw = process.stdout.read(size)
        terminator = process.stdout.read(1)
        if len(raw) != size or terminator != b"\n":
            raise ValueError(f"Truncated git cat-file response for {oid}")
        if size != expected_size:
            raise ValueError(
                f"Git blob size mismatch for {oid}: expected {expected_size}, got {size}"
            )
        if git_blob_oid(raw) != oid:
            raise ValueError(f"Git blob content does not hash to its manifest OID: {oid}")
        return raw
