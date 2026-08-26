"""Ingest captured Anthropic Messages API traffic.

One JSON object per line:

    {"session_id": ..., "request_id": ..., "ts": <epoch seconds>,
     "model": ..., "tools": [...], "system": [...], "messages": [...],
     "usage": {...}}

Capture it from an SDK middleware, a mitmproxy addon, or an OTel exporter --
the analysis never talks to a provider, so it runs offline and in CI.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..canonicalize import blocks_from_request
from ..model import RequestRecord, Usage


def record_from_payload(payload: dict) -> RequestRecord:
    u = payload.get("usage") or {}
    return RequestRecord(
        request_id=str(payload.get("request_id", "")),
        session_id=str(payload.get("session_id", "default")),
        ts=float(payload.get("ts", 0.0)),
        model=str(payload.get("model", "unknown")),
        blocks=blocks_from_request(payload),
        usage=Usage(
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0)),
        ),
    )


def _read_text(path: Path) -> str:
    """Read a trace, transparently handling gzip.

    Captured agent traffic is mostly large, highly repetitive prompts, so it
    compresses roughly 10:1. Reading .gz directly means a checked-in trace
    does not have to be unpacked before it can be profiled.
    """
    if path.suffix == ".gz":
        import gzip

        return gzip.decompress(path.read_bytes()).decode("utf-8")
    return path.read_text(encoding="utf-8")


def load_jsonl(path: str | Path) -> list[RequestRecord]:
    records = []
    for line in _read_text(Path(path)).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            records.append(record_from_payload(json.loads(line)))
    records.sort(key=lambda r: r.ts)
    return records
