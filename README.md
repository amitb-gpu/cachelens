# cachelens

[![ci](https://github.com/amitb-gpu/cachelens/actions/workflows/ci.yml/badge.svg)](https://github.com/amitb-gpu/cachelens/actions/workflows/ci.yml)

**Find what broke your LLM prompt cache — and what it cost.**

> **Observability tells you that your cache is missing. CacheLens tells you where
> it broke, why it broke, what it cost, and how to fix it.**

## What is CacheLens?

**CacheLens is a profiler that finds wasted LLM prompt-cache spend.**

It analyzes captured LLM requests turn by turn, finds where a reusable prompt
prefix stopped matching, identifies the likely cause, calculates the wasted
token cost, and recommends what to change.

It is designed for a simple question that ordinary cache metrics do not answer:

**What exactly broke my cache, and how much is that mistake costing me?**

## What problem does it solve?

LLM applications repeatedly send large amounts of identical context: system
prompts, tool definitions, instructions, conversation history, repository
context, and agent state. Prompt caching can make that repeated input much
cheaper.

But prompt caches are prefix-sensitive. **A tiny changing piece of a prompt can
cause thousands of otherwise reusable tokens to be rewritten.** A timestamp,
UUID, reordered tool schema, volatile DOM block, serialization change, or
misplaced cache breakpoint can invalidate a large stable prefix.

A hit-rate dashboard may tell you:

```text
cache hit rate: 52.6%
```

That tells you the cache missed. It generally does not tell you **exactly what
broke it, where the break occurred, whether it was avoidable, or what the
mistake costs**.

That is the problem CacheLens solves.

## What does CacheLens give me?

For each captured session, CacheLens gives you the things needed to act on a
cache miss rather than merely observe it:

- **the broken block and changed bytes** where the reusable prefix first diverged;
- **the likely root cause**, such as a timestamp, UUID, serialization drift,
  tool reordering, volatile block, or misplaced breakpoint;
- **the wasted tokens**, separated into stable content that should have been a
  cheap cache read and genuinely new content;
- **the dollar impact** for the session and a projection at your request volume;
- **a suggested fix** when the failure pattern is known;
- **machine-readable output and CI thresholds** for catching regressions.

The normal analysis is offline. It can run against live captures, historical
traces, or redacted traces shared in an issue.

## Who is it for?

CacheLens is useful for:

- **LLM infrastructure and inference engineers** reducing repeated-input cost;
- **agent-framework developers** building long-running, tool-using agents;
- **AI platform teams** operating Claude-backed applications at scale;
- **inference / FinOps engineers** trying to attribute LLM input-cost waste;
- **observability teams** that can see cache misses but need root-cause detail;
- **open-source maintainers** who want prompt-cache regressions to be
  reproducible and gateable in CI.

If your application sends short, independent prompts with little reusable
context, prompt-cache optimization may not materially affect your bill.

## Does it actually find anything?

**Yes.** CacheLens has been driven against the real prompt-assembly code of four
open-source agents at pinned commits, plus live traffic from a pre-fix OpenClaw
release.

| agent | finding |
|---|---|
| **browser-use** | **23–51% of the input bill was recoverable** across measured session shapes by splitting stable history from volatile page state. |
| **aider** | Mostly well behaved; repo-map re-ranking caused avoidable cache churn. |
| **gptme** | Clean in the captured run: zero prefix breaks in 9 turns. |
| **SWE-agent** | Clean in the captured run: zero prefix breaks in 7 turns. |
| **OpenClaw** | Reproduced the mechanism of public issue #75300 against live API traffic with genuine provider `usage` counters. |

For a representative 30-step browser-use session with ~13 KB page state,
exact provider token counts put the as-is input cost at **$0.8678** and the
split-block version at **$0.5289**: **39.1% recovered**. Across the measured
session shapes, the recoverable range was **23–51%**.

The clean gptme and SWE-agent runs matter too: when the prompt prefix remains
stable, CacheLens stays quiet.

Traces, capture harnesses, pinned commits, sensitivity results, and method limits
are in [`examples/real-agents/`](examples/real-agents/).

## Can I use it on my application?

**Yes.** The MVP includes a built-in Anthropic-compatible capture proxy, so you
can go from your own agent traffic to a CacheLens report in three commands.

### Install

```bash
pip install -e ".[dev]"
```

### Capture and analyze

```bash
# 1. start the capture proxy
cachelens proxy -o trace.jsonl

# 2. point your agent at it and run the agent normally
ANTHROPIC_BASE_URL=http://127.0.0.1:8788 <your agent command>

# 3. profile the captured session
cachelens trace.jsonl --req-per-day 30
```

The proxy needs **no credentials of its own**. The client's authorization header
passes through, so the API key stays with the application being profiled.

To capture request structure without forwarding requests to the provider or
spending tokens:

```bash
cachelens proxy -o trace.jsonl --no-forward
```

A typical report identifies the break, prices it, and suggests a fix:

```text
cachelens  session=agent-run-1  model=claude-sonnet-4-6  turns=12
==============================================================================
  reported cache hit rate      52.6%
  prefix breaks               11 of 11 turns  (11 avoidable)
  tokens rewritten needlessly 48,048
  new tokens billed as writes 99
  wasted spend (this session) $0.2742
  projected                   $22.43/month at 30 req/day

  root causes
     11x  VOLATILE_TIMESTAMP
     11x  MISPLACED_BREAKPOINT
      2x  LOOKBACK_EXCEEDED

  turn 1: msg_0000 -> msg_0001   at block 5 (system[0])
    4,377 tokens rewritten at 1h write rate
    !! VOLATILE_TIMESTAMP: system[0] changed because it contains a timestamp
       fix: move the timestamp out of the cached prefix and after the final
            cache breakpoint
```

### Useful commands

```bash
# machine-readable report
cachelens trace.jsonl --json

# fail CI on a critical finding or cost threshold
cachelens trace.jsonl --fail-on critical --max-wasted-usd 0.05

# use Anthropic's token-count endpoint instead of the byte heuristic
ANTHROPIC_API_KEY=sk-ant-... cachelens trace.jsonl --exact-tokens

# remove prompt contents while preserving trace shape
cachelens redact trace.jsonl -o trace.shape.jsonl.gz
```

Exact token counting is strictly opt-in. Merely having an API key in the
environment does not turn an offline analysis into a network call.

## Why cache misses can be expensive

For the currently modeled Anthropic cache economics, repeated input can be
charged at very different rates:

| | rate vs. base input |
|---|---|
| cache read | **0.10x** |
| cache write, 5m TTL | **1.25x** |
| cache write, 1h TTL | **2.00x** |

A stable token rewritten at the 5-minute write rate costs 12.5x what the same
token would have cost as a cache read. At the 1-hour write rate, the ratio is
20x.

CacheLens does **not** charge genuinely new content as though it could have been
a cache read. It separates stale tokens from novel tokens: stable content can
recover the write-vs-read gap, while genuinely new content can recover only the
write premium it never needed to pay.

Rates in `cost.py` are configurable. Unknown model rates are surfaced in the
report rather than silently treated as equally trustworthy; verify provider or
partner pricing before quoting dollar figures externally.

## What it detects

| Cause | What it means |
|---|---|
| `VOLATILE_TIMESTAMP` | A timestamp inside the cached prefix changes every request. |
| `OBJECT_REPR` | A Python object repr such as `0x7f9a...` leaked into a prompt. |
| `UUID_INJECTION` | A per-request UUID sits before the last breakpoint. |
| `TURN_COUNTER` | A per-turn counter is embedded in otherwise stable content. |
| `SERIALIZATION_DRIFT` | Semantically identical content serializes differently. |
| `TOOL_REORDER` | The same tool set appears in a different order, invalidating the prefix below it. |
| `MISPLACED_BREAKPOINT` | Volatile bytes sit inside the region a breakpoint is meant to cache. |
| `BREAKPOINT_ON_VOLATILE_BLOCK` | A partly stable, partly changing block is cached whole, forcing the stable portion to be rewritten. |
| `TOOL_TOKENS_UNCHANGED` | Tool serialization changed on the wire even though provider tokenization is unchanged. |
| `TTL_EXPIRY` | The prefix was valid, but the cache entry expired before reuse. |
| `BELOW_MIN_TOKENS` | The marked prefix is below the model's minimum cacheable length. |
| `LOOKBACK_EXCEEDED` | Too many blocks sit *after* the last breakpoint, pushing older cache entries outside the provider's lookback window. |
| `TOO_MANY_BREAKPOINTS` | More breakpoints are present than the API accepts. |
| `NO_BREAKPOINT` | Nothing is marked for caching. |
| `MODEL_SWITCH` | The model changed mid-session, invalidating the prefix. |

## How it works

1. **Canonicalize.** Flatten each request into provider wire order
   (`tools -> system -> messages`). Each block retains both its raw
   serialization and a canonical form.
2. **Build rolling prefix hashes.** CacheLens hashes progressively longer raw
   prefixes so it can find where consecutive requests stop matching.
3. **Find the first divergence.** Normal conversation growth is not a break;
   mutation of an existing cached prefix is.
4. **Attribute the change.** Diff the offending block and widen tiny character
   edits to useful surrounding context.
5. **Classify and price.** Match structural/textual patterns, split stale from
   novel content, and estimate the recoverable cost.

Large re-rendered blocks such as DOM state are handled with a line-first diff
path rather than feeding the whole block to a quadratic character matcher. That
change reduced a 30-step real-agent trace from **89.6s to 0.146s** during the
field study.

## Capturing and sharing traces

`cachelens proxy` writes JSONL directly:

```json
{"session_id":"s1","request_id":"msg_001","ts":1756200000.0,
 "model":"claude-sonnet-4-6","tools":[...],"system":[...],"messages":[...],
 "usage":{"cache_creation_input_tokens":9531,"cache_read_input_tokens":10638}}
```

An SDK middleware or OTel exporter can produce the same format. **Analysis of a
captured trace never calls a provider** unless `--exact-tokens` is explicitly
requested.

The proxy also handles a capture trap that can silently erase usage data:
provider SSE responses may be gzip-compressed when the client's
`Accept-Encoding` is passed through. The proxy strips that header upstream and
decompresses defensively on the way back before extracting usage counters.

### Redaction

```bash
cachelens redact trace.jsonl -o trace.shape.jsonl.gz
```

Redaction preserves block boundaries, byte lengths, `cache_control` placement,
and real `usage` fields while replacing prompt strings with same-length filler
derived from a hash of the original. Structural findings remain available;
textual rules no longer have the original text to match.

## Try the bundled reproduction

Two fixtures model a real cache-busting pattern: per-message metadata appears
inside a cached system block, forcing a large stable prefix to be rewritten on
every turn.

```bash
python examples/gen_fixtures.py
cachelens examples/openclaw_repro.jsonl --req-per-day 30   # 52.6% hit rate, $22.43/mo wasted
cachelens examples/fixed.jsonl          --req-per-day 30   # 99.8% hit rate, $0.00
```

## Field-study details

### browser-use: stable history and volatile DOM share one block

browser-use constructs a user message containing the task, accumulated agent
history, and current page state, then puts the message-level cache breakpoint on
that combined block. Because the live DOM changes every step, the block changes
every step.

That creates two losses: stable history is rewritten instead of read, and new
page state pays a cache-write premium for an entry that will not match on the
next step. Splitting the stable task/history from volatile page state makes the
former a growing cacheable prefix and leaves the DOM as ordinary new input.

Measured with exact provider token counts, the recoverable share ranged from
**23% to 51%** across the tested page-state sizes and session lengths.

### aider: mostly healthy, with repo-map churn

aider places its breakpoints so ordinary conversation growth usually happens
outside the cached region. Its repo map is different: it is re-ranked against
identifiers mentioned in the conversation and sits mid-prefix, so content can
shift even when no repository file changed. CacheLens reports that tradeoff
rather than treating the whole session as unhealthy.

### gptme and SWE-agent: clean controls

Both captured runs use a moving cache window that preserves readable prefixes
as the conversation grows. CacheLens reported zero prefix breaks across the
measured turns, providing a useful false-positive check for the profiler.

### OpenClaw: public bug mechanism reproduced with live usage

[openclaw#75300](https://github.com/openclaw/openclaw/issues/75300) described
volatile content inside a `cache_control`-marked system block. Live captures
against pre-fix **v2026.4.29** reproduced that mechanism through Dynamic Project
Context.

With content below the boundary held static, the captured session reported a
**97.0%** hit rate and zero breaks. When `HEARTBEAT.md` changed each turn, the
hit rate fell to **44.3%** and the system cache was rewritten on every turn:

```text
cache_read   14,457   14,457   14,457   <- tools held in cache
cache_write   9,686   10,322   10,964   <- system block rewritten
```

From the payload alone, CacheLens located the break at `system[0]`, raised
`BREAKPOINT_ON_VOLATILE_BLOCK`, and estimated the rewritten token counts within
roughly **2.5%** of the genuine provider counters.

This reproduces the **mechanism** in #75300, not the reporter's exact trigger.
The issue described per-message metadata on the channel path; the field capture
drove `agent --local` and triggered the same boundary failure through Dynamic
Project Context. The original scenario remains unreproduced here.

## Token-count calibration

The fallback character heuristic is intentionally reported with per-level
confidence rather than presented as exact:

| level | content | chars/token | heuristic error |
|---|---|---|---|
| `system` | instruction prose | 3.599 | **-0.02%** |
| `messages` | conversation prose | — | **+3.8%** |
| `messages` | serialized DOM | 2.926 | **-18.7%** |
| `tools` | JSON schemas | 3.220 | **-10.55%** |
| full payload | mixed | — | **-6.53%** |

`--exact-tokens` replaces the heuristic with Anthropic's token-count endpoint.
The browser-use economics above use exact provider counts.

### The provider does not tokenize the literal tool JSON you send

Measured on the same 30 tool definitions:

```text
compact  47,714 bytes -> 14,781 tokens
pretty   81,800 bytes -> 14,781 tokens
```

The 71% difference in wire bytes produced the same provider token count. This
matters because billing and cache identity are related but not identical:
provider tokenization can normalize tool definitions even though a changed wire
serialization can still invalidate the cache prefix. CacheLens therefore treats
serialization drift and its billing consequence as separate signals.

## Relationship to Anthropic cache diagnostics

Anthropic's cache diagnostics beta can return a live `cache_miss_reason` such as
`system_changed`. It is useful and complementary.

CacheLens focuses on the offline side:

- historical traces, not only live requests;
- byte/block-level attribution rather than only cache level;
- root-cause classification and fix suggestions;
- dollar estimates, projections, and CI thresholds;
- captured traffic from environments where the live diagnostic is unavailable.

If diagnostic metadata is present in a trace, CacheLens can consume it as an
additional signal rather than replacing it.

## Limits

- Four of the five field-study captures stop before a provider call and use
  scripted assistant replies. Their request bodies and cache-control placement
  come from the agents; their displayed provider hit-rate field is therefore
  not meaningful. OpenClaw is the live exception.
- browser-use page state in the field harness is synthetic and deliberately
  varied by size; the finding depends on stable-history versus volatile-state
  geometry, not a particular web page.
- Short scripted sessions do not exercise every retry, error, compaction, or
  long-session behavior found in production.
- Pricing changes. Dollar figures depend on the configured model rate and
  deployment channel.
- Exact token counting is currently implemented for Anthropic; OpenAI and OTel
  GenAI ingest are roadmap items.

## Roadmap

- [x] Prefix reconstruction, divergence detection, byte-level attribution
- [x] Rule-based root-cause classification with fix suggestions
- [x] Stale-vs-novel cost model and CI gate
- [x] Field study against real open-source agents
- [x] `cachelens redact` — share trace shape without prompt contents
- [x] Anthropic exact token counts via `--exact-tokens`
- [x] `cachelens proxy` — first-class live capture
- [ ] HTML context map: read / write / uncached bands with hover diff
- [ ] OpenAI and OTel GenAI ingest
- [ ] GitHub Action + pytest plugin

## License

Apache-2.0
