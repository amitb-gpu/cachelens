"""Strip a trace down to its shape so it can be shared.

A cache bug is a property of structure, not of wording: where the block
boundaries fall, which block carries the breakpoint, how many bytes sit on
each side, and which blocks changed between turns. None of that needs the
prompt text, and the prompt text is exactly what nobody wants to paste into
an issue tracker.

Every string value is replaced by a filler of the *same byte length* derived
from a hash of the original. Two consequences follow, and they are the whole
point:

  - lengths are preserved, so token estimates and dollar figures are unchanged
  - equal content hashes equal, so prefix divergence lands on the same block

What does not survive: the textual rules (VOLATILE_TIMESTAMP, UUID_INJECTION,
TURN_COUNTER) have nothing left to match on, and the evidence spans become
filler. Structural findings -- MISPLACED_BREAKPOINT, BREAKPOINT_ON_VOLATILE_BLOCK,
LOOKBACK_EXCEEDED, BELOW_MIN_TOKENS, TTL_EXPIRY -- are unaffected.

Tool ``name`` is kept: it is a short public identifier, it is what the report
labels blocks with, and losing it makes a shared trace much harder to read.
Descriptions and schema strings are redacted like everything else.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MARKER = "redacted"
# Keys whose values are structural rather than content, and are kept verbatim.
STRUCTURAL_KEYS = frozenset({
    "type", "role", "cache_control", "ttl", "name", "session_id",
    "request_id", "model", "ts", "usage", "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
})


def _filler_line(text: str) -> str:
    """A same-length stand-in for one line, stable under equality."""
    n = len(text)
    if n == 0:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tag = f"[{MARKER}:{digest[:12]}]"
    if n <= len(tag):
        return tag[:n]
    # Pad with a hash-derived pattern rather than a constant. A constant
    # would give two *different* originals a long common suffix, which is
    # exactly the signal stale/novel attribution reads -- padding one block
    # with dots made a browser-use trace report 192k stale tokens against a
    # true 82k.
    pattern = digest[12:28]
    span = n - len(tag)
    pad = (pattern * (span // len(pattern) + 1))[:span]
    return tag + pad


def filler(text: str) -> str:
    """Same-length stand-in, redacted line by line.

    Line granularity is what preserves the *inside* of a block: unchanged
    lines redact identically, so a growing history still presents a growing
    common prefix, and the stale/novel split lands where it did before.
    """
    if "\n" not in text:
        return _filler_line(text)
    return "\n".join(_filler_line(line) for line in text.split("\n"))


def _walk(obj: Any, key: str | None = None) -> Any:
    if isinstance(obj, str):
        if key in STRUCTURAL_KEYS:
            return obj
        return filler(obj)
    if isinstance(obj, dict):
        return {k: _walk(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, key) for v in obj]
    return obj


def redact_record(rec: dict) -> dict:
    """Redact one captured request, preserving shape, lengths and usage."""
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k in ("session_id", "request_id", "ts", "model", "usage"):
            out[k] = v
        elif k in ("tools", "system", "messages"):
            out[k] = _walk(v, k)
        else:
            out[k] = _walk(v, k)
    return out


def redact_trace(src: str | Path, dst: str | Path) -> tuple[int, int, int]:
    """Redact a JSONL trace. Returns (records, bytes_in, bytes_out)."""
    from .ingest.anthropic import _read_text

    src, dst = Path(src), Path(dst)
    lines = [l for l in _read_text(src).splitlines() if l.strip()
             and not l.startswith("#")]
    n_in = n_out = 0
    payloads = []
    for line in lines:
        rec = json.loads(line)
        n_in += len(line)
        red = json.dumps(redact_record(rec), ensure_ascii=False)
        n_out += len(red)
        payloads.append(red)

    text = "\n".join(payloads) + "\n"
    if dst.suffix == ".gz":
        import gzip

        dst.write_bytes(gzip.compress(text.encode("utf-8"), 9))
    else:
        dst.write_text(text, encoding="utf-8")
    return len(payloads), n_in, n_out
