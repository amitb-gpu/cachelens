"""Canonical data model shared by every ingest adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Provider cache hierarchy. Order matters: a change at level N invalidates
# level N and everything after it. (Anthropic: tools -> system -> messages.)
LEVELS = ("tools", "system", "messages")


@dataclass(frozen=True)
class Block:
    """One cacheable unit of a request, in wire order."""

    level: str          # "tools" | "system" | "messages"
    index: int          # position within its level
    kind: str           # "tool" | "text" | "message" | "image" | ...
    raw: str            # serialization as actually sent (what the provider hashes)
    canonical: str      # semantically normalized form (sorted keys, no whitespace)
    cache_control: dict[str, Any] | None = None
    label: str = ""     # human-readable name, e.g. tool name or "system[0]"

    @property
    def is_breakpoint(self) -> bool:
        return self.cache_control is not None

    @property
    def ttl(self) -> str:
        if not self.cache_control:
            return ""
        return str(self.cache_control.get("ttl", "5m"))


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def hit_rate(self) -> float:
        total = self.total_input
        return self.cache_read_input_tokens / total if total else 0.0


@dataclass
class RequestRecord:
    """One provider request, normalized."""

    request_id: str
    session_id: str
    ts: float                      # epoch seconds at request START (TTL is measured from here)
    model: str
    blocks: list[Block]
    usage: Usage = field(default_factory=Usage)

    def blocks_at(self, level: str) -> list[Block]:
        return [b for b in self.blocks if b.level == level]

    @property
    def breakpoints(self) -> list[int]:
        return [i for i, b in enumerate(self.blocks) if b.is_breakpoint]
