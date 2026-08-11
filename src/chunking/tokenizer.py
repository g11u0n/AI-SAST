"""The conservative byte counter locked by the Phase 1 experiment contract."""

from __future__ import annotations


TOKEN_COUNTER = "utf8_byte_upper_bound_v1"
TOKEN_COUNT_KIND = "conservative_upper_bound"


def utf8_budget_units(value: str) -> int:
    """Return the deterministic upper-bound units used for pre-inference gates."""

    return len(value.encode("utf-8"))


def decode_source(raw: bytes) -> tuple[str, str]:
    """Decode without replacement using the locked, deterministic policy."""

    if b"\x00" in raw:
        raise ValueError("NUL bytes are not accepted in C/C++ source blobs")
    try:
        return raw.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="strict"), "cp1252"


def rendered_slice(raw: bytes, encoding: str) -> str:
    if encoding not in {"utf-8", "cp1252"}:
        raise ValueError(f"Unsupported locked source encoding: {encoding}")
    return raw.decode(encoding, errors="strict")


def deterministic_windows(
    raw: bytes,
    *,
    encoding: str,
    max_rendered_utf8_bytes: int,
) -> list[tuple[int, int]]:
    """Split raw bytes at lines where possible and never inside a code point."""

    if not raw:
        return []
    if max_rendered_utf8_bytes <= 0:
        raise ValueError("max_rendered_utf8_bytes must be positive")

    text = rendered_slice(raw, encoding)
    encoded_widths = [len(char.encode("utf-8")) for char in text]
    raw_widths = [len(char.encode(encoding)) for char in text]
    windows: list[tuple[int, int]] = []
    start_char = 0
    start_raw = 0
    char_count = len(text)

    while start_char < char_count:
        used = 0
        cursor = start_char
        last_newline_after: int | None = None
        while cursor < char_count:
            width = encoded_widths[cursor]
            if used + width > max_rendered_utf8_bytes:
                break
            used += width
            cursor += 1
            if text[cursor - 1] == "\n":
                last_newline_after = cursor

        if cursor == start_char:
            raise ValueError("A single decoded character exceeds the source window budget")
        end_char = cursor
        if cursor < char_count and last_newline_after is not None:
            end_char = last_newline_after
        raw_length = sum(raw_widths[start_char:end_char])
        end_raw = start_raw + raw_length
        windows.append((start_raw, end_raw))
        start_char = end_char
        start_raw = end_raw

    if start_raw != len(raw):
        raise AssertionError("Decoded source windows do not reconstruct the raw blob")
    return windows
