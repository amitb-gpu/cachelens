"""Cost model.

Cache economics is a three-rate problem, not a token count:

    read   = 0.10x base input
    write  = 1.25x base input (5m TTL)  |  2.00x base input (1h TTL)

So a broken prefix does not merely fail to save you money -- it costs you
1.25x where you should have paid 0.10x. The waste multiple is 12.5x, and
that is the number worth putting in a CI gate.

Rates are USD per million input tokens and are overridable from a config
file; verify them against current provider pricing before quoting figures.
"""
from __future__ import annotations

from dataclasses import dataclass

BASE_INPUT_USD_PER_MTOK: dict[str, float] = {
    "claude-opus-5": 5.00,
    "claude-sonnet-4-6": 3.00,
    "claude-sonnet-4-5": 3.00,
    "claude-haiku-4-5": 1.00,
}
DEFAULT_BASE_RATE = 3.00

CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = {"5m": 1.25, "1h": 2.00}

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


def estimate_tokens(text: str) -> int:
    """Byte-length heuristic. Magnitude indicator, not a billing number.

    Swap in the provider's count_tokens endpoint for exact figures; the
    analysis is unchanged, only the precision of the dollar column.
    """
    return max(1, round(len(text) / 3.6))


@dataclass
class Waste:
    lost_tokens: int
    ttl: str
    base_usd_per_mtok: float

    @property
    def paid_usd(self) -> float:
        mult = CACHE_WRITE_MULT.get(self.ttl, 1.25)
        return self.lost_tokens / 1_000_000 * self.base_usd_per_mtok * mult

    @property
    def ideal_usd(self) -> float:
        return self.lost_tokens / 1_000_000 * self.base_usd_per_mtok * CACHE_READ_MULT

    @property
    def wasted_usd(self) -> float:
        return self.paid_usd - self.ideal_usd

    @property
    def multiple(self) -> float:
        return self.paid_usd / self.ideal_usd if self.ideal_usd else 0.0


def waste_for(lost_tokens: int, model: str, ttl: str = "5m") -> Waste:
    return Waste(lost_tokens, ttl or "5m", base_rate(model))
