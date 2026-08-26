"""Cost model.

Cache economics is a three-rate problem, not a token count:

    read   = 0.10x base input
    write  = 1.25x base input (5m TTL)  |  2.00x base input (1h TTL)

So a broken prefix does not merely fail to save you money -- it costs you
1.25x where you should have paid 0.10x. The waste multiple is 12.5x, and
that is the number worth putting in a CI gate.

Not every re-written byte was cacheable, though. Bytes that existed in the
previous request could have been a 0.10x read, so their waste multiple is
the full 12.5x. Bytes that are genuinely new this turn never had a cache
entry to hit; the most they could have been is ordinary 1.00x input, so
marking them cacheable costs only the 0.25x write premium. Charging both at
12.5x overstates the bill on agents whose cached region is one big volatile
block, which is exactly the shape real agent traffic tends to have.

Rates are USD per million input tokens and are overridable from a config
file; verify them against current provider pricing before quoting figures.
"""
from __future__ import annotations

from dataclasses import dataclass

from .tokens import CHARS_PER_TOKEN, HeuristicCounter, TokenCounter

BASE_INPUT_USD_PER_MTOK: dict[str, float] = {
    "claude-opus-5": 5.00,
    "claude-sonnet-4-6": 3.00,
    "claude-sonnet-4-5": 3.00,
    "claude-haiku-4-5": 1.00,
}
DEFAULT_BASE_RATE = 3.00

CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = {"5m": 1.25, "1h": 2.00}
# What uncached input costs: the best a genuinely-new byte could have done.
CACHE_MISS_MULT = 1.00

# Minimum prompt length below which nothing is cached at all.
MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    "claude-sonnet-5": 1024,
    "claude-opus-4-8": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-opus-4-1": 1024,
    "claude-opus-4-7": 2048,
    "claude-haiku-3-5": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
}
DEFAULT_MIN_CACHEABLE = 1024


def _norm(model: str) -> str:
    return model.lower().replace(".", "-").rsplit("-2", 1)[0].strip("-")


def base_rate(model: str) -> float:
    return BASE_INPUT_USD_PER_MTOK.get(_norm(model), DEFAULT_BASE_RATE)


def min_cacheable(model: str) -> int:
    return MIN_CACHEABLE_TOKENS.get(_norm(model), DEFAULT_MIN_CACHEABLE)


_DEFAULT_COUNTER = HeuristicCounter()


def estimate_tokens(text: str) -> int:
    """Byte-length heuristic, kept for callers that want no counter.

    Accuracy is per-level and measured, not uniform: see
    ``tokens.HEURISTIC_CONFIDENCE``. Pass an ExactCounter through
    ``analyze`` for provider-backed figures.
    """
    return _DEFAULT_COUNTER.count(text)


def tokens_from_chars(n_chars: int) -> int:
    """Same heuristic, for when only a character count is in hand."""
    return _DEFAULT_COUNTER.from_chars(n_chars)


@dataclass
class Waste:
    """Cost of one break, split by what the bytes could have cost instead.

    ``lost_tokens`` are stale: they were in the previous request, so a
    correctly-placed breakpoint would have made them a 0.10x read.
    ``novel_tokens`` are new this turn; their best case was 1.00x input,
    so all they lose is the write premium.
    """

    lost_tokens: int
    ttl: str
    base_usd_per_mtok: float
    novel_tokens: int = 0

    @property
    def write_mult(self) -> float:
        return CACHE_WRITE_MULT.get(self.ttl, 1.25)

    @property
    def rewritten_tokens(self) -> int:
        return self.lost_tokens + self.novel_tokens

    @property
    def paid_usd(self) -> float:
        return (
            self.rewritten_tokens / 1_000_000 * self.base_usd_per_mtok * self.write_mult
        )

    @property
    def ideal_usd(self) -> float:
        best = (
            self.lost_tokens * CACHE_READ_MULT
            + self.novel_tokens * CACHE_MISS_MULT
        )
        return best / 1_000_000 * self.base_usd_per_mtok

    @property
    def wasted_usd(self) -> float:
        return self.paid_usd - self.ideal_usd

    @property
    def multiple(self) -> float:
        return self.paid_usd / self.ideal_usd if self.ideal_usd else 0.0


def waste_for(
    lost_tokens: int, model: str, ttl: str = "5m", novel_tokens: int = 0
) -> Waste:
    return Waste(lost_tokens, ttl or "5m", base_rate(model), novel_tokens)


def stale_char_span(prev_raw: str, curr_raw: str) -> int:
    """Characters of ``curr_raw`` carried over unchanged from ``prev_raw``.

    Matching a common prefix and suffix is enough to find the boundary, and
    is far cheaper than a full diff on prompt-sized strings.
    """
    n = min(len(prev_raw), len(curr_raw))
    pre = 0
    while pre < n and prev_raw[pre] == curr_raw[pre]:
        pre += 1
    suf = 0
    limit = n - pre
    while suf < limit and prev_raw[-1 - suf] == curr_raw[-1 - suf]:
        suf += 1
    return min(pre + suf, len(curr_raw))


def split_stale_novel(
    prev_raw: str,
    curr_raw: str,
    counter: TokenCounter | None = None,
    level: str = "messages",
) -> tuple[int, int]:
    """Split a changed block into (stale, novel) token counts.

    A block that changes at all is re-written whole, so its unchanged bytes
    are still billed at the write rate. They are recoverable, though: split
    the block at the boundary and the stable side becomes a readable prefix.

    With an exact counter the block total is counted for real and then
    apportioned by the character split, since the provider counts whole
    blocks and the stale region is not contiguous.
    """
    stale_chars = stale_char_span(prev_raw, curr_raw)
    if counter is None or isinstance(counter, HeuristicCounter):
        c = counter or _DEFAULT_COUNTER
        return c.from_chars(stale_chars), c.from_chars(len(curr_raw) - stale_chars)
    total = counter.count(curr_raw, level)
    if not curr_raw:
        return 0, total
    stale = round(total * stale_chars / len(curr_raw))
    return stale, max(0, total - stale)
