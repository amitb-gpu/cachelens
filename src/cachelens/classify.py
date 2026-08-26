"""Root-cause classification.

A cache-miss token count tells you that you lost money. This tells you which
bytes cost you the money, what pattern they are an instance of, and what to
change. The rules below are each drawn from a cache bug seen in the wild.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

from .cost import split_stale_novel
from .model import Block
from .prefix import Divergence

TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"
    r"|\b\d{10,13}\b"
)
OBJECT_REPR_RE = re.compile(r"0x[0-9a-fA-F]{6,}")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
COUNTER_RE = re.compile(r"\b(?:turn|step|message|msg|iteration|seq)[ _#:-]*\d+\b", re.I)


@dataclass
class Cause:
    code: str
    severity: str            # "critical" | "high" | "medium" | "info"
    detail: str
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""


def _expand_to_lines(text: str, lo: int, hi: int, pad: int = 80) -> tuple[int, int]:
    """Widen a changed region to whole lines so the evidence carries context.

    A minimal character diff reports \'0\' -> \'1\'; that is true and useless.
    The line it sits on ("timestamp: 2026-...") is what names the bug.
    """
    ls = text.rfind("\n", 0, lo) + 1
    le = text.find("\n", hi)
    if le == -1:
        le = len(text)
    if le - ls > 4 * pad:  # unbroken wall of text: fall back to a padded window
        ls, le = max(0, lo - pad), min(len(text), hi + pad)
    return ls, le


# Contested-middle size above which character diffing is abandoned for line
# diffing. SequenceMatcher is quadratic in sequence length, so a block whose
# middle genuinely differs -- a re-rendered DOM, a fresh repo map -- costs
# tens of seconds at character granularity. Lines are ~50x coarser, and the
# evidence is widened to line boundaries anyway, so nothing legible is lost.
_CHAR_DIFF_BUDGET = 1200


def _line_offsets(text: str) -> tuple[list[str], list[int]]:
    """Split into lines (keeping ends) alongside each line's char offset."""
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    offsets.append(pos)
    return lines, offsets


def _opcode_regions(a_mid: str, b_mid: str) -> list[tuple[int, int, int, int]]:
    """Changed (i1, i2, j1, j2) char spans of the contested middle.

    Diffs by character while that is affordable, by line when it is not.
    """
    if max(len(a_mid), len(b_mid)) <= _CHAR_DIFF_BUDGET:
        sm = difflib.SequenceMatcher(None, a_mid, b_mid, autojunk=False)
        return [
            (i1, i2, j1, j2)
            for tag, i1, i2, j1, j2 in sm.get_opcodes()
            if tag != "equal"
        ]

    a_lines, a_off = _line_offsets(a_mid)
    b_lines, b_off = _line_offsets(b_mid)
    sm = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    return [
        (a_off[i1], a_off[i2], b_off[j1], b_off[j2])
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    ]


def _changed_spans(a: str, b: str, limit: int = 6, merge_gap: int = 60) -> list[str]:
    """The substrings that actually differ, widened to legible context.

    Adjacent edits closer than merge_gap are merged, so one logically-single
    change ("msg_0009" -> "msg_0010") is reported once rather than as three
    separate character edits.
    """
    # SequenceMatcher is quadratic, and prompts are large and mostly identical.
    # Peel the shared head and tail first; only the contested middle is diffed.
    head = 0
    n = min(len(a), len(b))
    while head < n and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < n - head and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]:
        tail += 1
    a_mid, b_mid = a[head:len(a) - tail], b[head:len(b) - tail]

    regions: list[list[int]] = []
    for i1, i2, j1, j2 in _opcode_regions(a_mid, b_mid):
        i1, i2, j1, j2 = i1 + head, i2 + head, j1 + head, j2 + head
        if regions and i1 - regions[-1][1] <= merge_gap:
            regions[-1][1] = max(regions[-1][1], i2)
            regions[-1][3] = max(regions[-1][3], j2)
        else:
            regions.append([i1, i2, j1, j2])

    spans: list[str] = []
    for i1, i2, j1, j2 in regions[:limit]:
        al, ar = _expand_to_lines(a, i1, i2)
        bl, br = _expand_to_lines(b, j1, j2)
        old, new = a[al:ar].strip(), b[bl:br].strip()
        spans.append(f"{old[:120]!r} -> {new[:120]!r}")
    return spans


def _semantically_equal(a: Block, b: Block) -> bool:
    return a.canonical == b.canonical and a.raw != b.raw


def classify(
    div: Divergence,
    prev_blocks: list[Block],
    curr_blocks: list[Block],
    gap_seconds: float,
    prev_model: str,
    curr_model: str,
) -> list[Cause]:
    causes: list[Cause] = []

    if prev_model != curr_model:
        causes.append(
            Cause(
                "MODEL_SWITCH",
                "high",
                f"Model changed {prev_model} -> {curr_model}; the entire prefix is invalidated.",
                suggestion="Pin the model per session, or accept the reseed cost knowingly.",
            )
        )

    # Structural checks that apply whether or not the prefix broke.
    causes.extend(_structural_checks(curr_blocks))

    if not div.diverged:
        if gap_seconds > 0:
            causes.extend(_ttl_checks(curr_blocks, gap_seconds))
        return causes

    a, b = div.prev_block, div.curr_block
    assert a is not None and b is not None
    spans = _changed_spans(a.raw, b.raw)
    joined = " ".join(spans)

    if _semantically_equal(a, b):
        if a.level == "tools":
            # Two separate facts, and they do not travel together. The prefix
            # hash is over the bytes you sent, so the cache really does miss.
            # The bill is over the provider's own re-rendering of the schema,
            # which is identical either way -- measured: the same tool set at
            # 47,714 B compact and 81,800 B pretty-printed both count 14,781
            # tokens. So this costs a full rewrite of unchanged content.
            causes.append(
                Cause(
                    "SERIALIZATION_DRIFT",
                    "critical",
                    f"{b.label} serializes differently while describing the same tool. "
                    "The prefix hash is taken over the bytes you sent, so the cache "
                    "misses and every block below is re-written.",
                    spans,
                    "Serialize tool definitions with stable key ordering "
                    "(json.dumps(..., sort_keys=True)) before handing them to the SDK.",
                )
            )
            causes.append(
                Cause(
                    "TOOL_TOKENS_UNCHANGED",
                    "info",
                    "Billed tokens for this block did not change: the provider "
                    "re-renders tool schemas into its own format before tokenizing, "
                    "so key order and whitespace cost nothing. The whole rewrite is "
                    "therefore recoverable -- none of it is new content.",
                    suggestion="Fix the ordering and this block returns to a cache "
                    "read at no loss of information to the model.",
                )
            )
        else:
            causes.append(
                Cause(
                    "SERIALIZATION_DRIFT",
                    "critical",
                    f"{b.label} is semantically identical to the previous turn but serializes "
                    "differently. This is pure waste: nothing about the request actually changed.",
                    spans,
                    "Serialize with stable key ordering (json.dumps(..., sort_keys=True)) "
                    "before handing payloads to the SDK.",
                )
            )
    elif a.level == "tools" and _tool_set_reordered(prev_blocks, curr_blocks):
        causes.append(
            Cause(
                "TOOL_REORDER",
                "critical",
                "The tool set is unchanged but its order differs. Tools sit at the very "
                "front of the prefix, so this invalidates every level below it.",
                spans,
                "Sort tool definitions by name before every request. If tools come from a "
                "dict or a set, the iteration order is not stable across processes.",
            )
        )
    else:
        for regex, code, label, fix in (
            (OBJECT_REPR_RE, "OBJECT_REPR", "a Python object memory address",
             "The repr of an object leaked into the prompt (e.g. an un-serialized "
             "few-shot example). Format it explicitly instead of relying on str()."),
            (TIMESTAMP_RE, "VOLATILE_TIMESTAMP", "a timestamp",
             "Move the timestamp out of the cached prefix and into the last user "
             "message, after the final cache breakpoint."),
            (UUID_RE, "UUID_INJECTION", "a UUID",
             "Per-request identifiers must live after the last cache breakpoint."),
            (COUNTER_RE, "TURN_COUNTER", "a per-turn counter",
             "Drop the counter or move it after the last cache breakpoint."),
        ):
            if regex.search(joined):
                causes.append(
                    Cause(
                        code,
                        "critical",
                        f"{b.label} changed because it contains {label}, which varies every "
                        "request while the surrounding content is stable.",
                        spans,
                        fix,
                    )
                )
                break
        else:
            causes.append(
                Cause(
                    "CONTENT_CHANGED",
                    "info" if b.level == "messages" else "medium",
                    f"{b.label} ({b.level}) changed substantively.",
                    spans,
                    "Expected for the growing tail of a conversation; suspicious in tools "
                    "or system, which should be stable for the whole session.",
                )
            )

    # A breakpoint sitting on a block that is partly stable and partly rewritten
    # every turn. No regex will catch this -- the tell is structural: the block
    # carries a large unchanged region that gets re-written anyway because the
    # breakpoint cannot split it. This is the shape real agent traffic takes when
    # a whole conversation is rendered into one text block.
    # Only as a fallback: when a named textual cause fired, MISPLACED_BREAKPOINT
    # below already reports this same geometry, and saying it twice is noise.
    if (
        div.diverged
        and not any(c.severity == "critical" for c in causes)
        and _before_last_breakpoint(div.index, curr_blocks)
    ):
        stale, novel = split_stale_novel(a.raw, b.raw)
        total = stale + novel
        if total and stale >= 200 and stale / total >= 0.20:
            where = (
                f"The cache breakpoint sits directly on {b.label}, which changes every turn."
                if b.is_breakpoint
                else f"{b.label} changes every turn and sits inside the region covered by "
                     f"the breakpoint at block {_last_bp(curr_blocks)}."
            )
            causes.append(
                Cause(
                    "BREAKPOINT_ON_VOLATILE_BLOCK",
                    "high",
                    f"{where} About {stale:,} of its ~{total:,} tokens are unchanged from "
                    f"last turn ({stale / total:.0%}), but a block is cached whole, so they "
                    "are re-written at the write rate instead of read at the read rate.",
                    spans,
                    suggestion="Split this block in two: put the stable part (task, "
                    "instructions, accumulated history) in its own content part with the "
                    "breakpoint on it, and leave the volatile part after it, unmarked.",
                )
            )

    # The openclaw class of bug: volatile bytes sitting *before* a breakpoint.
    if any(c.severity == "critical" for c in causes) and _before_last_breakpoint(
        div.index, curr_blocks
    ):
        causes.append(
            Cause(
                "MISPLACED_BREAKPOINT",
                "critical",
                f"The volatile content is at block {div.index}, which is *inside* the region "
                f"covered by the cache breakpoint at block {_last_bp(curr_blocks)}. Every "
                "stable byte after it is being rewritten each turn for nothing.",
                suggestion="Split the block: keep the stable part before the breakpoint and "
                "move the volatile part after it. This is usually a one-line reorder "
                "with an outsized payoff.",
            )
        )

    return causes


def _last_bp(blocks: list[Block]) -> int:
    bps = [i for i, blk in enumerate(blocks) if blk.is_breakpoint]
    return bps[-1] if bps else -1


def _before_last_breakpoint(index: int | None, blocks: list[Block]) -> bool:
    last = _last_bp(blocks)
    return index is not None and last >= 0 and index <= last


def _tool_set_reordered(prev: list[Block], curr: list[Block]) -> bool:
    p = sorted(b.canonical for b in prev if b.level == "tools")
    c = sorted(b.canonical for b in curr if b.level == "tools")
    seq_p = [b.canonical for b in prev if b.level == "tools"]
    seq_c = [b.canonical for b in curr if b.level == "tools"]
    return p == c and seq_p != seq_c


def _structural_checks(blocks: list[Block]) -> list[Cause]:
    out: list[Cause] = []
    bps = [i for i, b in enumerate(blocks) if b.is_breakpoint]
    if len(bps) > 4:
        out.append(
            Cause(
                "TOO_MANY_BREAKPOINTS",
                "high",
                f"{len(bps)} cache breakpoints declared; the API accepts at most 4.",
                suggestion="Keep breakpoints at the stable seams: end of tools, end of "
                "system, and the last stable message.",
            )
        )
    if bps:
        gap = len(blocks) - 1 - bps[-1]
        if gap > 20:
            out.append(
                Cause(
                    "LOOKBACK_EXCEEDED",
                    "high",
                    f"{gap} blocks sit after the last breakpoint; the lookback window checks "
                    "at most 20 positions, so older entries become unreachable.",
                    suggestion="Add a rolling breakpoint near the conversation tail.",
                )
            )
    if not bps:
        out.append(
            Cause(
                "NO_BREAKPOINT",
                "high",
                "No cache_control breakpoint in this request; nothing is being cached.",
                suggestion="Mark the last stable block (usually the end of the system "
                "prompt) with cache_control.",
            )
        )
    return out


TTL_SECONDS = {"5m": 300, "1h": 3600, "30m": 1800, "24h": 86400}


def _ttl_checks(blocks: list[Block], gap_seconds: float) -> list[Cause]:
    out: list[Cause] = []
    for b in blocks:
        if not b.is_breakpoint:
            continue
        ttl = TTL_SECONDS.get(b.ttl, 300)
        if gap_seconds > ttl:
            out.append(
                Cause(
                    "TTL_EXPIRY",
                    "high",
                    f"The prefix was unchanged, but {gap_seconds:.0f}s elapsed since the "
                    f"previous request and the breakpoint at {b.label} has a {b.ttl} TTL. "
                    "The entry expired before it could be reused.",
                    suggestion="Either raise the TTL to 1h for long-lived sessions, or "
                    "accept the reseed. Note the clock starts when the *previous* request "
                    "began, so a slow response eats into the window.",
                )
            )
            break
    return out
