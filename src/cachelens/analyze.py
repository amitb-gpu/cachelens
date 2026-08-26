"""Session-level analysis: walk consecutive requests, attribute every break."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .classify import Cause, classify
from .cost import estimate_tokens, min_cacheable, split_stale_novel, waste_for
from .model import RequestRecord
from .prefix import Divergence, cacheable_prefix_end, first_divergence


@dataclass
class Break:
    prev_id: str
    curr_id: str
    turn: int
    divergence: Divergence
    causes: list[Cause]
    lost_tokens: int      # stale bytes re-written; a read was available for these
    wasted_usd: float
    ttl: str
    novel_tokens: int = 0  # new bytes inside the cached region; only the write premium

    @property
    def rewritten_tokens(self) -> int:
        return self.lost_tokens + self.novel_tokens

    @property
    def worst_severity(self) -> str:
        order = ["critical", "high", "medium", "info"]
        for s in order:
            if any(c.severity == s for c in self.causes):
                return s
        return "info"


@dataclass
class SessionReport:
    session_id: str
    model: str
    turns: int
    breaks: list[Break] = field(default_factory=list)
    reported_hit_rate: float = 0.0
    total_wasted_usd: float = 0.0
    total_lost_tokens: int = 0
    total_novel_tokens: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def actual_breaks(self) -> list[Break]:
        """Turn transitions where the prefix genuinely stopped matching."""
        return [b for b in self.breaks if b.divergence.diverged]

    @property
    def avoidable_breaks(self) -> list[Break]:
        """Breaks with a named, fixable root cause -- the actionable subset."""
        return [b for b in self.actual_breaks if b.worst_severity in ("critical", "high")]

    @property
    def advisories(self) -> list[Break]:
        """Findings that cost money without breaking the prefix (TTL, structure)."""
        return [
            b for b in self.breaks
            if not b.divergence.diverged
            and b.worst_severity in ("critical", "high")
        ]

    @property
    def cause_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = defaultdict(int)
        for b in self.breaks:
            for c in b.causes:
                hist[c.code] += 1
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))

    def projected_monthly_usd(self, requests_per_day: float) -> float:
        if self.turns < 2:
            return 0.0
        per_request = self.total_wasted_usd / (self.turns - 1)
        return per_request * requests_per_day * 30.0


def analyze_session(records: list[RequestRecord]) -> SessionReport:
    if not records:
        raise ValueError("no records")

    model = records[-1].model
    rep = SessionReport(
        session_id=records[0].session_id, model=model, turns=len(records)
    )

    read = sum(r.usage.cache_read_input_tokens for r in records)
    total = sum(r.usage.total_input for r in records)
    rep.reported_hit_rate = read / total if total else 0.0

    floor = min_cacheable(model)

    for turn, (prev, curr) in enumerate(zip(records, records[1:]), start=1):
        div = first_divergence(prev.blocks, curr.blocks)
        gap = max(0.0, curr.ts - prev.ts)
        causes = classify(div, prev.blocks, curr.blocks, gap, prev.model, curr.model)

        bp_end = cacheable_prefix_end(curr.blocks)
        lost = 0
        novel = 0
        if div.diverged and bp_end >= div.index:
            # Every block from the break to the end of the cached region is
            # re-written. Split each one by what it could have cost instead:
            # bytes carried over from last turn had a read available, bytes
            # that are new this turn had, at best, ordinary input pricing.
            for i in range(div.index, bp_end + 1):
                blk = curr.blocks[i]
                prev_blk = prev.blocks[i] if i < len(prev.blocks) else None
                if prev_blk is None or prev_blk.level != blk.level:
                    novel += estimate_tokens(blk.raw)
                elif prev_blk.raw == blk.raw:
                    # Unchanged block dragged into the rewrite by a break
                    # above it -- the purest form of this waste.
                    lost += estimate_tokens(blk.raw)
                else:
                    stale_t, novel_t = split_stale_novel(prev_blk.raw, blk.raw)
                    lost += stale_t
                    novel += novel_t

        prefix_tokens = sum(
            estimate_tokens(b.raw) for b in curr.blocks[: bp_end + 1]
        ) if bp_end >= 0 else 0
        if bp_end >= 0 and prefix_tokens < floor:
            causes.append(
                Cause(
                    "BELOW_MIN_TOKENS",
                    "high",
                    f"The marked prefix is ~{prefix_tokens} tokens, under the {floor}-token "
                    f"minimum for {model}. It is never cached, breakpoint or not.",
                    suggestion="Either grow the stable prefix past the floor or stop "
                    "paying the breakpoint's bookkeeping cost.",
                )
            )
            lost = 0
            novel = 0

        ttl = next((b.ttl for b in curr.blocks if b.is_breakpoint), "5m")
        w = waste_for(lost, model, ttl, novel)
        rep.breaks.append(
            Break(prev.request_id, curr.request_id, turn, div, causes, lost,
                  w.wasted_usd, ttl, novel)
        )
        rep.total_lost_tokens += lost
        rep.total_novel_tokens += novel
        rep.total_wasted_usd += w.wasted_usd

    return rep


def analyze(records: list[RequestRecord]) -> list[SessionReport]:
    sessions: dict[str, list[RequestRecord]] = defaultdict(list)
    for r in records:
        sessions[r.session_id].append(r)
    return [analyze_session(sorted(v, key=lambda r: r.ts)) for v in sessions.values()]
