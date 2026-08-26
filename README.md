# cachelens

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

```bash
# profile a captured session
cachelens trace.jsonl --req-per-day 30

# machine-readable
cachelens trace.jsonl --json

# gate it in CI
cachelens trace.jsonl --fail-on critical --max-wasted-usd 0.05
```

Input is JSONL, one captured request per line:

```json
{"session_id":"s1","request_id":"msg_001","ts":1756200000.0,
 "model":"claude-sonnet-4-6","tools":[...],"system":[...],"messages":[...],
 "usage":{"cache_creation_input_tokens":9531,"cache_read_input_tokens":10638}}
```

Capture it from an SDK middleware, a mitmproxy addon, or an OTel exporter.
**The analysis never calls a provider**, so it runs offline, in CI, and against
traces recorded months ago.

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
- [ ] HTML context map: per-turn bands colored read / write / uncached, hover diff
- [ ] OpenAI and OTel GenAI (`gen_ai.usage.cache_read.input_tokens`) ingest
- [ ] `cachelens proxy` — live mitmproxy capture, point any agent at it
- [ ] GitHub Action + pytest plugin
- [ ] Exact token counts via provider `count_tokens` endpoints

## Caveats

Token counts use a byte-length heuristic and are a magnitude indicator, not a
billing number; swap in a provider `count_tokens` call for exact figures. Rates
in `cost.py` are configurable and should be verified against current pricing
before you quote a number to anyone.

## License

Apache-2.0
