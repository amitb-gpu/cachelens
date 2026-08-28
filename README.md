# cachelens

[![ci](https://github.com/amitb-gpu/cachelens/actions/workflows/ci.yml/badge.svg)](https://github.com/amitb-gpu/cachelens/actions/workflows/ci.yml)

**A profiler for LLM prompt-cache economics.** Observability tools tell you your
cache hit rate. `cachelens` tells you *which bytes broke the prefix, what that
cost, and what to change.*

```
cachelens  session=agent-run-1  model=claude-sonnet-4-6  turns=12
==============================================================================
  reported cache hit rate      52.6%
  prefix breaks               11 of 11 turns  (11 avoidable)
  tokens rewritten needlessly 48,147
  wasted spend (this session) $0.2744
  projected                   $22.45/month at 30 req/day

  root causes
     11x  VOLATILE_TIMESTAMP
     11x  MISPLACED_BREAKPOINT
      2x  LOOKBACK_EXCEEDED

  turn timeline   . = prefix held   X = prefix broke
       1  XXXXXXXXXXX

------------------------------------------------------------------------------
  turn 1: msg_0000 -> msg_0001   at block 5 (system[0])
    4,377 tokens rewritten at 1h write rate = 20x what a cache read would have cost
    !! VOLATILE_TIMESTAMP: system[0] changed because it contains a timestamp,
       which varies every request while the surrounding content is stable.
         'message_id: msg_0000\ntimestamp: 2026-08-26T09:20:00Z'
      -> 'message_id: msg_0001\ntimestamp: 2026-08-26T09:20:45Z'
         fix: Move the timestamp out of the cached prefix and into the last
              user message, after the final cache breakpoint.
    !! MISPLACED_BREAKPOINT: The volatile content is inside the region covered
       by the cache breakpoint. Every stable byte after it is being rewritten
       each turn for nothing.
```

## Why this exists

Prompt caching is a three-rate problem, not a token count:

| | rate vs. base input |
|---|---|
| cache read | **0.10x** |
| cache write, 5m TTL | **1.25x** |
| cache write, 1h TTL | **2.00x** |

So a broken prefix does not merely fail to save money. It costs **12.5x to 20x**
what the same tokens would have cost as a cache read. A single stray timestamp
in a system prompt can quietly convert a 90%-discount path into a 25%-premium
one, and the only symptom is a hit-rate number that looks vaguely low.

Finding the stray timestamp is the hard part. That is what this does.

## What it detects

| Cause | What it means |
|---|---|
| `VOLATILE_TIMESTAMP` | A timestamp inside the cached prefix changes every request |
| `OBJECT_REPR` | A Python object repr (`0x7f9a...`) leaked into a prompt, usually via an un-serialized few-shot example |
| `UUID_INJECTION` | A per-request UUID sits before the last breakpoint |
| `TURN_COUNTER` | A per-turn counter is embedded in stable content |
| `SERIALIZATION_DRIFT` | Two requests are semantically identical but serialize differently (unstable dict ordering) |
| `TOOL_REORDER` | Same tool set, different order. Tools sit at the front of the prefix, so this invalidates everything below |
| `MISPLACED_BREAKPOINT` | Volatile bytes sit *inside* the region a breakpoint is meant to cache |
| `BREAKPOINT_ON_VOLATILE_BLOCK` | A block that is part stable and part rewritten sits under a breakpoint. Blocks cache whole, so the stable half is re-written every turn. Structural: fires with no textual tell |
| `TOOL_TOKENS_UNCHANGED` | Companion to `SERIALIZATION_DRIFT` on tools: the cache missed, but the provider re-renders schemas before tokenizing, so the billed content never changed and the whole rewrite is recoverable |
| `TTL_EXPIRY` | The prefix was fine; the entry expired before reuse |
| `BELOW_MIN_TOKENS` | The marked prefix is under the model's minimum cacheable length, so it was never cached at all |
| `LOOKBACK_EXCEEDED` | More than 20 blocks after the last breakpoint, past the lookback window |
| `TOO_MANY_BREAKPOINTS` | More than the 4 the API accepts |
| `NO_BREAKPOINT` | Nothing is being cached |
| `MODEL_SWITCH` | Model changed mid-session, invalidating the whole prefix |

## Install

```bash
pip install -e ".[dev]"
```

## Use

Two steps: record traffic, then profile it.

```bash
# 1. record. point your agent's base URL at the proxy and drive it as usual
cachelens proxy -o trace.jsonl
#    ANTHROPIC_BASE_URL=http://127.0.0.1:8788  <your agent command>

# 2. profile
cachelens trace.jsonl --req-per-day 30
```

The proxy forwards every request upstream unchanged and needs **no credentials
of its own** — the client's auth header passes straight through, so the key
stays with the agent you are profiling. `--no-forward` records the shape of
requests and returns a stub instead of calling the provider, which captures a
trace without spending anything.

```bash
# profile a captured session
cachelens trace.jsonl --req-per-day 30

# machine-readable
cachelens trace.jsonl --json

# gate it in CI
cachelens trace.jsonl --fail-on critical --max-wasted-usd 0.05

# exact token counts instead of the byte heuristic (free endpoint, needs a key)
ANTHROPIC_API_KEY=sk-ant-... cachelens trace.jsonl --exact-tokens

# strip a trace to its shape so it can be shared in an issue
cachelens redact trace.jsonl -o trace.shape.jsonl.gz
```

`redact` keeps block boundaries, byte lengths, `cache_control` placement and the
real `usage` fields, and replaces every prompt string with same-length filler
derived from a hash of the original. Token totals reproduce within ~0.1% and
structural findings are unchanged; the textual rules have nothing left to match,
and a finding sitting exactly on a threshold can flip.

Input is JSONL, one captured request per line:

```json
{"session_id":"s1","request_id":"msg_001","ts":1756200000.0,
 "model":"claude-sonnet-4-6","tools":[...],"system":[...],"messages":[...],
 "usage":{"cache_creation_input_tokens":9531,"cache_read_input_tokens":10638}}
```

`cachelens proxy` writes this format directly. An SDK middleware or an OTel
exporter can produce it too. **The analysis never calls a provider**, so it runs
offline, in CI, and against traces recorded months ago.

## Try it

Two fixtures ship with the repo, modeled on a real cache bug found in a widely
used open-source agent: per-message metadata was appended to the system prompt
*before* the `cache_control` marker, so a ~9.5k-token stable block was rewritten
on every single turn.

```bash
python examples/gen_fixtures.py
cachelens examples/openclaw_repro.jsonl --req-per-day 30   # 52.6% hit rate, $22.45/mo wasted
cachelens examples/fixed.jsonl          --req-per-day 30   # 99.8% hit rate, $0.00
```

## How it works

1. **Canonicalize.** Flatten each request into wire-order blocks honoring the
   provider's cache hierarchy (`tools -> system -> messages`). Each block keeps
   two serializations: `raw` (what the provider hashes) and `canonical` (sorted
   keys, for detecting semantically-null changes).
2. **Rolling prefix hash.** `H_i = sha256(H_{i-1} || raw_i)`. Cheap, and mirrors
   what the provider actually does.
3. **First divergence.** Scan consecutive requests for the first index where the
   hashes disagree. Everything after that point is a write that should have been
   a read. A *growing* conversation is not a break, and is not reported as one.
4. **Byte-level attribution.** Diff the offending block, merge adjacent edits,
   and widen to line boundaries. A minimal character diff reports `'0' -> '1'`,
   which is true and useless; the enclosing line names the bug.
5. **Classify and price.** Match the changed spans against the rule table above,
   then compute `lost_tokens x (write_rate - read_rate)`.

The diff peels the shared head and tail before running `SequenceMatcher`, since
prompts are large and mostly identical. On the bundled fixtures this took the
suite from **55.7s to 0.12s**.

## Relationship to Anthropic's cache diagnostics

Anthropic ships a [cache diagnostics beta](https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics)
(`cache-diagnosis-2026-04-07`) that returns a `cache_miss_reason` such as
`system_changed`. It is genuinely useful and you should turn it on. It is also:

- **live-only** — you must pass `previous_message_id` on the request; it cannot
  analyze traffic you already recorded
- **level-granular** — it tells you *system changed*, not *which 40 bytes*
- **Claude API only** — not on Bedrock or Vertex
- **not priced** — no dollar figure, no trend, no CI gate

`cachelens` is the offline half: it works from captured traffic, names the exact
bytes and the pattern they belong to, prices the waste, and fails your build
when a pull request makes it worse. Where diagnostics data is present in a
trace, it is consumed as an additional signal rather than replaced.

## Roadmap

- [x] Prefix reconstruction, divergence detection, byte-level attribution
- [x] Rule-based root-cause classification with fix suggestions
- [x] Cost model and CI gate
- [x] Field study against real open-source agents (see `examples/real-agents/`)
- [ ] HTML context map: per-turn bands colored read / write / uncached, hover diff
- [x] `cachelens redact` — share a trace's shape without its prompts
- [~] Exact token counts via provider `count_tokens` (done for Anthropic via
      `--exact-tokens`; per-level confidence reported when falling back)
- [x] `cachelens proxy` — live capture as a first-class command
- [ ] OpenAI and OTel GenAI ingest
- [ ] GitHub Action + pytest plugin

## Field results

Run against four open-source agents at pinned commits, driving each agent's own
prompt-assembly code rather than a fixture:

| agent | verdict |
|---|---|
| **browser-use** | Task, full history and live DOM share one block, with the only message-level breakpoint on it. **23-51% of the input bill** is recoverable by splitting that block in two |
| **aider** | Mostly well behaved. The repo map is re-ranked every turn and sits mid-prefix, invalidating the history below it on 2 of 8 turns |
| **gptme** | Clean. Zero prefix breaks in 9 turns |
| **SWE-agent** | Clean. Zero prefix breaks in 7 turns |

The browser-use range is measured with exact provider token counts, not the
heuristic. An earlier revision of this table said 25-55%; counting the DOM
blocks exactly moved every cell down by 1.7-4.8 points, because serialized DOM
is far denser than the heuristic assumes and dense novel content recovers at
only 0.25x where stale content recovers at 1.15x. See **Calibration**.

Traces, capture harnesses and the method's limits are in
[`examples/real-agents/`](examples/real-agents/). Those four captures never
reached a provider, so their `reported cache hit rate` reads 0.0%; every other
figure is computed from the request bodies.

### openclaw: the bug class reproduced, with real usage

[openclaw#75300](https://github.com/openclaw/openclaw/issues/75300) reports an
Anthropic prefix busted every turn by volatile content sitting inside a
`cache_control`-marked system block. It was closed as "already implemented" by
a bot; the reporter rebutted twice with a mitmproxy intercept and was not
answered. We captured live traffic from pre-fix **v2026.4.29** to settle it.

**The defect reaches the wire.** openclaw marks where the split should happen
with an `<!-- OPENCLAW_CACHE_BOUNDARY -->` sentinel, and its own payload policy
splits there. The `pi-ai` harness bundled in 2026.4.29 builds its own Anthropic
request and never consults the marker. In our capture the marker arrives at the
provider *inside* the single `cache_control` block, at char 27,075 of 29,237 —
with everything openclaw itself labels "Dynamic Project Context" sitting after
it, inside the cached region. `pi-ai` is absent from 2026.5.28, which is why
the bug goes away there.

**Two sessions, and the difference between them is the whole point.**

| session | volatile content changed? | reported hit rate | breaks |
|---|---|---|---|
| `..._live` | no | **97.0%** | 0 of 4 |
| `..._heartbeat` | yes (`HEARTBEAT.md`, per turn) | **44.3%** | 3 of 3 |

The first is a true negative: nothing below the boundary changed, so nothing
broke, and reporting zero breaks is correct. The second changes `HEARTBEAT.md`
between turns — which is that file's documented purpose — and the #75300
signature appears immediately in the genuine `usage` counters:

```
cache_read  14,457   14,457   14,457     <- tools, correctly cached, never grows
cache_write  9,686   10,322   10,964     <- system block, re-written every turn
```

Against the issue's published figures (read ~10,638 constant, write ~9,531
every turn) the structure matches exactly: tools cached, system busted, write
roughly constant regardless of conversation length.

From the payload alone, cachelens located the break at `system[0]`, raised
`BREAKPOINT_ON_VOLATILE_BLOCK`, and quoted the offending bytes — reporting that
8,089 of ~8,098 tokens in the block were unchanged and re-written anyway. Its
predicted rewrite lands within **-2.5%** of the real counters:

| turn | predicted | real `cache_creation` | error |
|---|---|---|---|
| 1 | 9,407 | 9,686 | -2.9% |
| 2 | 10,067 | 10,322 | -2.5% |
| 3 | 10,733 | 10,964 | -2.1% |

**What this is and is not.** It reproduces the *mechanism* of #75300 — the
boundary marker ignored, volatile content inside the cached block — through a
different trigger. The originally reported trigger was per-message
`message_id`/`timestamp` on the **channel** path; we drove `agent --local` and
triggered it through Dynamic Project Context instead. The reporter's exact
scenario remains unreproduced by us. The defect they described is present.

Both traces are committed redacted (see below).

## Calibration

Token accuracy is per-level and measured, not uniform. Against
`/v1/messages/count_tokens` on real captured agent traffic:

| level | content | chars/token | heuristic error |
|---|---|---|---|
| `system` | instruction prose | 3.599 | **-0.02%** |
| `messages` | conversation prose | — | **+3.8%** |
| `messages` | serialized DOM | 2.926 | **-18.7%** |
| `tools` | JSON schemas | 3.220 | **-10.55%** |
| full payload | mixed | — | **-6.53%** |

The divisor in `cost.py` is 3.600, which is why prose lands within a rounding
error and everything denser does not. Two structural effects sit inside the
tools figure: a fixed **~496-token preamble** charged once whenever any tool is
present, and content that the heuristic cannot see (below).

`cachelens --exact-tokens` replaces the heuristic with provider counts (the
endpoint bills nothing; it needs `ANTHROPIC_API_KEY`). Every report prints which
counter produced its numbers and the confidence for each level.

**Exact counting is strictly opt-in, and the CI gate never needs a key.** A key
merely present in the environment does not change what a run does — only the
flag does. That is deliberate: a fork whose CI exports `ANTHROPIC_API_KEY` for
unrelated reasons would otherwise turn a plain `cachelens trace.jsonl` in its
pipeline into a live-call run, and inherit a red badge for a reason that appears
nowhere in the command line. The workflow asserts this.

**Where the error lands matters.** It concentrates at the `tools` level, which
is also the level that breaks least often — tools are stable across a session
by construction. The bugs that actually cost money break at `system` or
`messages` level, and those are the levels where the heuristic is measured
rather than modelled. A -10.55% error on a block that never invalidates costs
nothing.

Rates in `cost.py` are first-party API prices per million input tokens and are
configurable. A model with no entry is priced at the $3.00 default and the
report says so explicitly — Bedrock and Vertex are partner-operated and priced
separately, so override the table if you bill there.

## The provider does not tokenize the JSON you send

Measured, and worth stating on its own because most people guess otherwise:

```
same 30 tool definitions, compact  47,714 bytes -> 14,781 tokens
same 30 tool definitions, pretty   81,800 bytes -> 14,781 tokens
```

A 71% difference in wire bytes, byte-identical token count. The provider parses
tool definitions and re-renders them into its own internal format before
tokenizing, so whitespace and key order in your request body cost exactly
nothing.

Two consequences, which pull in opposite directions:

- **Billing.** No choice of characters-per-token divisor can be correct for
  tool definitions, because the bytes measured are not the object billed. This
  is why the token counter is pluggable rather than tuned.
- **Caching.** The prefix hash *is* taken over the bytes you sent. So
  reordering keys in a tool schema invalidates the cache while changing the
  bill not at all. `cachelens` reports these as two findings —
  `SERIALIZATION_DRIFT` for the invalidation, `TOOL_TOKENS_UNCHANGED` to record
  that the whole rewrite is recoverable content rather than new content.

## Capturing your own traffic

`cachelens proxy` is the least invasive capture: point the agent's base URL at
it and it forwards upstream. It is worth knowing what it handles for you, since
this is the trap that produces a silently wrong trace rather than an error — if
you pass the client's `Accept-Encoding` through, the provider may gzip the SSE
stream, and a parser reading it as text finds no `message_start` event. The
usage counters then come back as `0`, which looks like "caching is off" instead
of "the capture is broken". We strip `Accept-Encoding` on the way out and
decompress defensively on the way back. If you write your own capture, do both.

## License

Apache-2.0
