"""Turn analysis objects into JSON-safe dicts.

Pure projection. Nothing here computes anything the analyzer did not already
compute -- if a number appears below, it came off a ``SessionReport``,
``Break`` or ``Cause`` unchanged. Keeping that rule is what lets the HTTP
layer and the CLI disagree about presentation while agreeing about facts.

Two shapes, for two very different consumers:

``full``     everything, for a browser page that can scroll.
``compact``  budgeted for an agent tool response. WebMCP guidance puts tool
             output in the ~1.5K character range, and a full per-break dump
             of the bundled browser-use trace is 60,321 characters. So the
             compact shape groups causes, returns a handful of representative
             breaks, and truncates evidence rather than letting a caller
             discover the limit the hard way.
"""
from __future__ import annotations

from typing import Any

from .analyze import Break, SessionReport
from .classify import Cause

# Roughly the per-tool-output budget WebMCP guidance suggests. Enforced as a
# hard trim so a response is never silently truncated somewhere downstream.
TOOL_OUTPUT_BUDGET = 1500
EVIDENCE_CHARS = 160
DEFAULT_BREAK_LIMIT = 3


# Written by cachelens.redact into every string it replaces.
REDACTION_MARKER = "[redacted:"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_redacted(text: str) -> bool:
    return REDACTION_MARKER in str(text)


def cause_dict(cause: Cause, evidence_chars: int = EVIDENCE_CHARS) -> dict[str, Any]:
    """Project one cause, flagging evidence that has been redacted.

    A redacted trace still diagnoses correctly -- block boundaries, lengths
    and divergence all survive -- but its evidence spans are hash filler.
    Showing that filler as "the bytes that broke your cache" is worse than
    saying plainly that the content was removed, so callers get a flag
    rather than having to sniff for the marker themselves.
    """
    redacted = any(is_redacted(e) for e in cause.evidence)
    return {
        "code": cause.code,
        "severity": cause.severity,
        "detail": cause.detail,
        "evidence": [] if redacted else [_clip(e, evidence_chars) for e in cause.evidence],
        "evidence_redacted": redacted,
        "fix": cause.suggestion,
    }


def break_dict(brk: Break, evidence_chars: int = EVIDENCE_CHARS) -> dict[str, Any]:
    div = brk.divergence
    block = div.curr_block
    return {
        "turn": brk.turn,
        "from_request": brk.prev_id,
        "to_request": brk.curr_id,
        "diverged": div.diverged,
        "block_index": div.index,
        "block_label": getattr(block, "label", None),
        "level": div.level,
        "severity": brk.worst_severity,
        "stale_tokens": brk.lost_tokens,
        "novel_tokens": brk.novel_tokens,
        "wasted_usd": round(brk.wasted_usd, 6),
        "ttl": brk.ttl,
        "causes": [cause_dict(c, evidence_chars) for c in brk.causes],
    }


def _confidence(rep: SessionReport) -> dict[str, Any]:
    return {
        level: {"basis": c.basis, "error_pct": c.error_pct, "note": c.note}
        for level, c in rep.level_confidence.items()
    }


def summary_dict(rep: SessionReport, requests_per_day: float = 0.0) -> dict[str, Any]:
    out: dict[str, Any] = {
        "session_id": rep.session_id,
        "model": rep.model,
        "turns": rep.turns,
        "reported_hit_rate": round(rep.reported_hit_rate, 4),
        "has_provider_usage": rep.reported_hit_rate > 0,
        "breaks": len(rep.actual_breaks),
        "avoidable_breaks": len(rep.avoidable_breaks),
        "stale_tokens": rep.total_lost_tokens,
        "novel_tokens": rep.total_novel_tokens,
        "wasted_usd": round(rep.total_wasted_usd, 6),
        "causes": rep.cause_histogram,
        "token_counter": rep.counter_name,
        "level_confidence": _confidence(rep),
        "notes": list(rep.notes),
    }
    if requests_per_day:
        out["projected_monthly_usd"] = round(
            rep.projected_monthly_usd(requests_per_day), 4
        )
    return out


def report_dict(rep: SessionReport, requests_per_day: float = 0.0) -> dict[str, Any]:
    """Everything, for the browser page."""
    out = summary_dict(rep, requests_per_day)
    out["breaks_detail"] = [break_dict(b) for b in rep.actual_breaks]
    out["advisories"] = [break_dict(b) for b in rep.advisories]
    return out


def grouped_causes(rep: SessionReport) -> list[dict[str, Any]]:
    """Causes rolled up, with the turns they fired on.

    The bundled browser-use trace has 29 breaks and exactly 2 distinct cause
    codes. Listing every break buries that; grouping surfaces it.
    """
    groups: dict[str, dict[str, Any]] = {}
    for brk in rep.actual_breaks:
        for cause in brk.causes:
            g = groups.setdefault(cause.code, {
                "code": cause.code,
                "severity": cause.severity,
                "occurrences": 0,
                "turns": [],
                "detail": cause.detail,
                "fix": cause.suggestion,
            })
            g["occurrences"] += 1
            g["turns"].append(brk.turn)
    order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    return sorted(
        groups.values(),
        key=lambda g: (order.get(g["severity"], 9), -g["occurrences"]),
    )


def representative_breaks(
    rep: SessionReport,
    cause_code: str | None = None,
    limit: int = DEFAULT_BREAK_LIMIT,
) -> list[Break]:
    """The costliest breaks, optionally filtered to one cause."""
    breaks = rep.actual_breaks
    if cause_code:
        wanted = cause_code.upper()
        breaks = [b for b in breaks if any(c.code == wanted for c in b.causes)]
    return sorted(breaks, key=lambda b: -b.wasted_usd)[: max(1, limit)]


def explain_dict(
    rep: SessionReport,
    cause_code: str | None = None,
    limit: int = DEFAULT_BREAK_LIMIT,
) -> dict[str, Any]:
    """Summary + grouped causes + a few representative breaks."""
    chosen = representative_breaks(rep, cause_code, limit)
    return {
        "summary": summary_dict(rep),
        "causes": grouped_causes(rep),
        "filtered_to_cause": cause_code.upper() if cause_code else None,
        "representative_breaks": [break_dict(b) for b in chosen],
        "breaks_shown": len(chosen),
        "breaks_total": len(rep.actual_breaks),
    }


def estimate_split_dict(rep: SessionReport, turn: int) -> dict[str, Any]:
    """The one counterfactual this tool can support honestly.

    It is not a simulation of arbitrary prompt reorganization. It answers a
    single, narrow question about a single break:

        if the block that broke were split so its unchanged part sat before
        the cache breakpoint and its changed part after, what would this turn
        have cost instead?

    That question is answerable because the analyzer already separates the
    two. Stale tokens were present last turn, so a correctly placed
    breakpoint would have made them a cache read. Novel tokens are new this
    turn; the best they could ever have been is ordinary uncached input.
    Both numbers come off the Break unchanged -- the arithmetic below is the
    cost model's own, not a second opinion.

    What it does not do: predict a cache-hit rate, model any other edit, or
    account for a fix changing what the model sees and therefore does.
    """
    from .cost import CACHE_MISS_MULT, CACHE_READ_MULT, base_rate, waste_for

    match = next((b for b in rep.actual_breaks if b.turn == turn), None)
    if match is None:
        available = [b.turn for b in rep.actual_breaks]
        return {
            "error": "no_break_at_turn",
            "message": f"turn {turn} is not a break in this trace",
            "breaks_at_turns": available,
        }

    w = waste_for(match.lost_tokens, rep.model, match.ttl, match.novel_tokens)
    rate = base_rate(rep.model)
    return {
        "turn": match.turn,
        "block_label": getattr(match.divergence.curr_block, "label", None),
        "block_index": match.divergence.index,
        "stale_tokens": match.lost_tokens,
        "novel_tokens": match.novel_tokens,
        "ttl": match.ttl,
        "rate_usd_per_mtok": rate,
        "write_multiplier": w.write_mult,
        "current_usd": round(w.paid_usd, 6),
        "if_split_usd": round(w.ideal_usd, 6),
        "saving_usd": round(w.wasted_usd, 6),
        "assumption": (
            f"Splitting this block would let {match.lost_tokens:,} unchanged tokens "
            f"be read at {CACHE_READ_MULT}x instead of written at {w.write_mult}x. "
            f"The {match.novel_tokens:,} tokens that are new this turn were never "
            f"cacheable and are priced at ordinary {CACHE_MISS_MULT}x input."
        ),
        "not_modelled": (
            "This is one block, this turn. It does not predict a cache-hit rate, "
            "model any other change, or account for a fix altering model behaviour."
        ),
        "token_counter": rep.counter_name,
    }
