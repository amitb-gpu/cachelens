"""Token counting, and how much to trust it.

Two counters share one interface. The heuristic one divides bytes by a
constant and needs no credentials. The exact one asks the provider's
``count_tokens`` endpoint, which is free to call but needs an API key.

The reason this is pluggable rather than tuned is a measurement: the same
tool definitions serialized compactly (47,714 B) and pretty-printed
(81,800 B) both count 14,781 tokens. The provider re-renders tool schemas
into its own format and discards the JSON you sent, so byte-counting tool
definitions measures an object that never reaches the tokenizer. No choice
of divisor fixes that -- only asking the provider does.

Calibration below is measured, not assumed. See README "Calibration".
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol

CHARS_PER_TOKEN = 3.6


@dataclass(frozen=True)
class Confidence:
    """How far a level's token count can be trusted, and why."""

    basis: str          # "exact" | "measured" | "modelled"
    error_pct: float | None   # signed calibration error, heuristic vs provider
    note: str

    @property
    def label(self) -> str:
        if self.basis == "exact":
            return "exact"
        if self.error_pct is None:
            return "modelled"
        digits = 2 if abs(self.error_pct) < 1 else 1
        return f"{self.error_pct:+.{digits}f}%"


# Measured against /v1/messages/count_tokens on real captured agent traffic.
# tools carries no error figure on purpose: the quantity the heuristic
# measures is not the quantity billed, so a single number would imply a
# precision that does not exist.
HEURISTIC_CONFIDENCE: dict[str, Confidence] = {
    "system": Confidence(
        "measured", -0.02,
        "instruction prose; measured at 3.599 chars/token against a 3.600 divisor",
    ),
    "messages": Confidence(
        "measured", None,
        "content-dependent: prose measures +3.8%, serialized DOM -18.7% "
        "(2.926 chars/token). Treat as +/-20% until counted exactly.",
    ),
    "tools": Confidence(
        "modelled", None,
        "JSON schemas are re-rendered by the provider before tokenizing, and a "
        "fixed ~496-token tool preamble is charged once when any tool is "
        "present. Measured -10.55% low on a 30-tool set; no divisor corrects "
        "this in general.",
    ),
}

EXACT_CONFIDENCE = Confidence("exact", 0.0, "provider count_tokens")


class TokenCounter(Protocol):
    def count(self, text: str, level: str = "messages") -> int: ...
    def confidence(self, level: str) -> Confidence: ...


class HeuristicCounter:
    """Byte-length estimate. No credentials, no network, no exactness."""

    name = "heuristic"

    def __init__(self, chars_per_token: float = CHARS_PER_TOKEN):
        self.chars_per_token = chars_per_token

    def count(self, text: str, level: str = "messages") -> int:
        return self.from_chars(len(text))

    def from_chars(self, n_chars: int) -> int:
        return max(0, round(n_chars / self.chars_per_token))

    def confidence(self, level: str) -> Confidence:
        return HEURISTIC_CONFIDENCE.get(level, HEURISTIC_CONFIDENCE["messages"])


class ExactCounter:
    """Provider-backed counts via /v1/messages/count_tokens.

    The endpoint bills no tokens, but it is a network round trip, so results
    are memoized per process. Falls back to the heuristic on any failure
    rather than aborting an analysis mid-run.
    """

    name = "exact"
    ENDPOINT = "https://api.anthropic.com/v1/messages/count_tokens"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5"):
        self.api_key = api_key
        self.model = model
        self._cache: dict[str, int] = {}
        self._fallback = HeuristicCounter()
        self._base: int | None = None
        self._degraded = False

    def _call(self, text: str) -> int:
        body = json.dumps(
            {"model": self.model,
             "messages": [{"role": "user", "content": text or "x"}]},
            separators=(",", ":"),
        ).encode()
        req = urllib.request.Request(
            self.ENDPOINT, data=body,
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["input_tokens"]

    def count(self, text: str, level: str = "messages") -> int:
        if self._degraded:
            return self._fallback.count(text, level)
        key = f"{level}\x00{text}"
        if key in self._cache:
            return self._cache[key]
        try:
            if self._base is None:
                self._base = self._call("x")
            n = max(0, self._call(text) - self._base)
        except Exception:
            # One failure degrades the whole run, so the mixed counts in a
            # single report never come from two different bases.
            self._degraded = True
            return self._fallback.count(text, level)
        self._cache[key] = n
        return n

    def confidence(self, level: str) -> Confidence:
        if self._degraded:
            return self._fallback.confidence(level)
        return EXACT_CONFIDENCE


def get_counter(exact: bool | None = None, model: str = "claude-sonnet-4-5") -> TokenCounter:
    """Exact when a key is available and not refused, heuristic otherwise."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if exact is False or (exact is None and not key) or (exact and not key):
        return HeuristicCounter()
    return ExactCounter(key, model)
