"""Prefix reconstruction and divergence detection.

The provider caches a *prefix*: block 0..i must be byte-identical for the
entry at breakpoint i to be reused. So the only question that matters is
"what is the first index at which two requests stop agreeing" -- everything
after that point is a cache write instead of a cache read.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .model import Block


def prefix_hashes(blocks: list[Block]) -> list[str]:
    """Rolling prefix hash: H_i = sha256(H_{i-1} || level || raw_i)."""
    out: list[str] = []
    h = hashlib.sha256()
    for b in blocks:
        h = h.copy()
        h.update(b.level.encode())
        h.update(b"\x00")
        h.update(b.raw.encode("utf-8"))
        out.append(h.hexdigest())
    return out


@dataclass
class Divergence:
    """Where two consecutive requests stopped sharing a prefix."""

    index: int | None          # first non-matching block index; None if identical
    level: str | None
    prev_block: Block | None
    curr_block: Block | None
    shared_blocks: int         # count of blocks that did match
    reason: str                # "identical" | "block_changed" | "truncated" | "appended"

    @property
    def diverged(self) -> bool:
        return self.index is not None


def first_divergence(prev: list[Block], curr: list[Block]) -> Divergence:
    ph, ch = prefix_hashes(prev), prefix_hashes(curr)
    n = min(len(ph), len(ch))
    for i in range(n):
        if ph[i] != ch[i]:
            return Divergence(
                index=i,
                level=curr[i].level,
                prev_block=prev[i],
                curr_block=curr[i],
                shared_blocks=i,
                reason="block_changed",
            )
    # Common prefix intact. Growing the conversation is normal and healthy.
    if len(ch) > len(ph):
        return Divergence(None, None, None, None, n, "appended")
    if len(ch) < len(ph):
        return Divergence(None, None, None, None, n, "truncated")
    return Divergence(None, None, None, None, n, "identical")


def cacheable_prefix_end(blocks: list[Block]) -> int:
    """Index of the last block covered by a cache breakpoint, or -1."""
    bps = [i for i, b in enumerate(blocks) if b.is_breakpoint]
    return bps[-1] if bps else -1
