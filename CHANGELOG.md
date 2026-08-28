# Changelog

## Unreleased

### Corrections to previously published figures

Two numbers this project published were wrong. Both were found by measuring
against real traffic rather than fixtures, and both are recorded here rather
than quietly edited.

- **Waste was overstated by 2.3x.** The cost model priced every re-written
  token as though a cache read had been available. For a block whose content
  is genuinely new each turn — a re-rendered DOM, a fresh page of results —
  that is false: a byte that did not exist last turn could at best have been
  ordinary 1.00x input, so marking it cacheable loses 0.25x, not 12.5x. Breaks
  are now split into stale and novel tokens and priced separately. A 12-step
  browser-use session fell from $0.0612 to $0.0268.

- **The token error factor was revised from -1.17% to -6.53%.** The first
  figure was derived from token counts published in an issue report and was
  optimistic. Measured directly against `/v1/messages/count_tokens` on a real
  captured payload, the byte heuristic runs **-6.53%** low overall: -0.02% on
  instruction prose, -10.55% on tool definitions.

- **The browser-use recoverable range was restated from 25-55% to 23-51%.**
  That range rested on message blocks made of serialized DOM, which the
  heuristic reads at 3.6 chars/token when it actually tokenizes at 2.926 — an
  18.7% undercount. Recomputing all six sensitivity cells with exact counts
  moved every one down by 1.7-4.8 points. The finding stands; the numbers were
  too generous.

### Added

- `cachelens proxy` — record live traffic by sitting in front of the provider.
  Point the agent's base URL at it and drive the agent normally. It needs no
  credentials of its own: the client's auth header passes straight through, so
  the key stays with the agent being profiled. `--no-forward` captures request
  shape without calling the provider or spending anything. This closes the gap
  that made the rest of the tool hard to actually use — the analysis was only
  ever as good as the traffic you could capture, and capturing it was the part
  people gave up on.

- `cachelens redact` — writes a shape-only copy of a trace: block boundaries,
  byte lengths, `cache_control` placement and real `usage` survive; every
  prompt string is replaced by same-length filler derived from a hash of the
  original. Redaction is line-wise, which is what preserves a growing common
  prefix and therefore the stale/novel split inside a block.
- `--exact-tokens` — count via the provider's `count_tokens` endpoint instead
  of the byte heuristic. The endpoint bills nothing but needs an API key.
  Strictly opt-in: a key present in the environment never changes what a run
  does, so the CI gate stays offline and green for forks that happen to export
  one. Asserted by the workflow and by two tests.
- Pluggable token counting (`cachelens.tokens`), with per-level confidence
  reported in every run instead of one global caveat.
- `BREAKPOINT_ON_VOLATILE_BLOCK` — structural rule for a breakpoint sitting on
  a block that is part stable and part rewritten. Fires on shape, so it catches
  cases no textual rule can see.
- `TOOL_TOKENS_UNCHANGED` — companion finding that separates a cache
  invalidation from a billing event when a tool schema only re-serializes.
- Transparent `.gz` trace loading.
- Field study against aider, browser-use, gptme and SWE-agent, plus two live
  openclaw captures with genuine `usage`. See README "Field results".

### Fixed

- Pricing table was missing every current model except four. `claude-fable-5`
  and `claude-mythos-5` ($10.00/MTok) were billed at the $3.00 default -- a
  3.3x understatement -- along with Opus 4.6/4.7/4.8 ($5.00) and Sonnet 5
  ($2.00). Rates added, and a model with no published rate now says so in the
  report instead of quietly defaulting.

- `get_counter()` selected the exact counter whenever `ANTHROPIC_API_KEY` was
  set, making network use implicit rather than opt-in. A plain `cachelens`
  invocation in a fork's CI would have started making live calls.
- Redaction fidelity is now a test rather than a thing someone noticed. The
  first redactor was out by 2.3x on stale tokens (81,941 reported as 192,427)
  and nothing failed; the guard asserts agreement within 0.5% on the
  browser-use trace, and pins the threshold-flip caveat alongside it.

- **Line-level diffing above a 1,200-character contested region.**
  `SequenceMatcher` is quadratic, and the existing head/tail peel only helps
  when a change is localized. A re-rendered DOM put ~30 KB of genuinely
  different characters into the matcher: 45 seconds per break, 89.6s for a
  30-turn trace. Now 0.146s, a ~600x speedup, with no loss of evidence quality
  because spans were already widened to line boundaries before display.
- `SERIALIZATION_DRIFT` no longer counts a re-serialized tool schema as new
  content. The provider re-renders tool definitions before tokenizing, so the
  billed tokens are unchanged and the rewrite is entirely recoverable.

### Documented

- **The provider does not tokenize the JSON you send.** The same 30 tool
  definitions at 47,714 bytes compact and 81,800 bytes pretty-printed both
  count 14,781 tokens. Whitespace and key order in the request body cost
  nothing at billing time — but the prefix hash is still taken over those
  bytes, so key order does invalidate the cache.
- Calibration table, per level, measured rather than assumed.
- The gzip/SSE capture trap: forwarding `Accept-Encoding` through a recording
  proxy lets the provider gzip the stream, and a text parser then finds no
  `message_start`, silently reporting `write=0 read=0` — which reads as
  "caching is off" rather than "the capture is broken".
- openclaw #75300's mechanism reproduced against the live API on pre-fix
  v2026.4.29, with genuine `usage`. The `<!-- OPENCLAW_CACHE_BOUNDARY -->`
  sentinel reaches the provider *inside* the single `cache_control` block
  (char 27,075 of 29,237), because the bundled `pi-ai` harness builds its own
  request and never consults it. With volatile content below the boundary held
  static: 97.0% hit rate, zero breaks. With it changing per turn: 44.3%, three
  breaks of three, and the issue's signature in the counters — `cache_read`
  pinned at 14,457 while `cache_write` runs 9,686/10,322/10,964. cachelens
  located the break at `system[0]` from the payload alone and predicted the
  rewrite within -2.5% of the real counters. Different trigger from the
  reported one (Dynamic Project Context on `--local`, not channel
  `message_id`/`timestamp`), same defective path.

## 0.1.0

- Prefix reconstruction, divergence detection, byte-level break attribution.
- 13 classification rules with fix suggestions.
- Cost model and CI gate.
