"""Plain-text report. No dependencies, pipes cleanly, readable in CI logs."""
from __future__ import annotations

from ..analyze import SessionReport
from ..cost import CACHE_READ_MULT, CACHE_WRITE_MULT

SEV_MARK = {"critical": "!!", "high": " !", "medium": " ~", "info": "  "}


def render(rep: SessionReport, requests_per_day: float = 0.0, verbose: bool = True) -> str:
    L: list[str] = []
    add = L.append

    add(f"cachelens  session={rep.session_id}  model={rep.model}  turns={rep.turns}")
    add("=" * 78)
    add(f"  reported cache hit rate     {rep.reported_hit_rate:6.1%}")
    add(f"  prefix breaks               {len(rep.actual_breaks)} of {len(rep.breaks)} turns"
        f"  ({len(rep.avoidable_breaks)} avoidable)")
    if rep.advisories:
        add(f"  structural advisories       {len(rep.advisories)}")
    add(f"  tokens rewritten needlessly {rep.total_lost_tokens:,}")
    if rep.total_novel_tokens:
        add(f"  new tokens billed as writes {rep.total_novel_tokens:,}"
            f"  (never re-read; write premium only)")
    add(f"  wasted spend (this session) ${rep.total_wasted_usd:,.4f}")
    if requests_per_day:
        add(f"  projected                   ${rep.projected_monthly_usd(requests_per_day):,.2f}"
            f"/month at {requests_per_day:g} req/day")
    add("")
    if rep.level_confidence:
        add(f"  token counts: {rep.counter_name}")
        for lvl, c in rep.level_confidence.items():
            add(f"    {lvl:<9} {c.label:>7}   {c.note}")
        add("")

    for note in rep.notes:
        add(f"  !! {note}")
    if rep.notes:
        add("")

    if rep.cause_histogram:
        add("  root causes")
        for code, n in rep.cause_histogram.items():
            add(f"    {n:3d}x  {code}")
        add("")

    add("  turn timeline   . = prefix held   X = prefix broke")
    strip = "".join("X" if b.divergence.diverged else "." for b in rep.breaks)
    for i in range(0, len(strip), 60):
        add(f"    {i+1:>4}  {strip[i:i+60]}")
    add("")

    if not verbose:
        return "\n".join(L)

    shown = 0
    for b in rep.breaks:
        interesting = [c for c in b.causes if c.severity in ("critical", "high")]
        if not interesting:
            continue
        shown += 1
        if shown > 8:
            add(f"  ... {len(rep.avoidable_breaks) - 8} more avoidable breaks")
            break
        d = b.divergence
        where = (f"block {d.index} ({d.curr_block.label})" if d.diverged
                 else "no structural break")
        add("-" * 78)
        add(f"  turn {b.turn}: {b.prev_id} -> {b.curr_id}   at {where}")
        if b.lost_tokens:
            mult = CACHE_WRITE_MULT.get(b.ttl, 1.25) / CACHE_READ_MULT
            add(f"    {b.lost_tokens:,} tokens rewritten at {b.ttl} write rate "
                f"= {mult:.0f}x what a cache read would have cost  "
                f"(${b.wasted_usd:.4f} wasted)")
        if b.novel_tokens:
            premium = CACHE_WRITE_MULT.get(b.ttl, 1.25) - 1.0
            add(f"    {b.novel_tokens:,} tokens are new this turn: no read was "
                f"available, so marking them costs the {premium:.2f}x write premium")
        for c in interesting:
            add(f"    {SEV_MARK[c.severity]} {c.code}: {c.detail}")
            for e in c.evidence[:3]:
                add(f"         {e}")
            if c.suggestion:
                add(f"         fix: {c.suggestion}")
    return "\n".join(L)
