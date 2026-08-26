# Real-agent traces

Captured prompt-cache traffic from four open-source agents, plus the harnesses
that produced it. These are the inputs behind the Phase 3 findings.

## What is real and what is not

Every trace was produced by **the agent's own prompt-assembly code**, imported
from a pinned commit and driven to the point where it hands a request body to
the provider. Nothing about the request shape, block ordering, or
`cache_control` placement is reconstructed by hand -- those are the decisions
under test, so they had to come from the agent itself.

Two things are stand-ins, and they are the reason no figure here should be
quoted as a measured bill:

- **Model replies are scripted.** No API key was used, so an interception
  point returns canned assistant output instead of calling the provider.
  Control flow around it is the agent's own.
- **Token counts are the byte heuristic** in `cost.py`, not provider
  `count_tokens` output. They are magnitude indicators. Swap in exact counts
  before quoting a number to anyone.

For `browser-use`, the `BrowserStateSummary` is also synthetic: a stand-in for
a live page, sized to the middle of browser-use's own range (its DOM listing
cap is 40,000 characters). The finding does not depend on the page contents,
only on the ratio of accumulated history to per-step state, which the
sensitivity table in the writeup varies deliberately.

## openclaw: a live capture, and a redacted one

`traces/openclaw_2026-4-29_live.jsonl.gz` is different from the other four. It
is a **real 5-turn session against the Anthropic API** from pre-fix openclaw
v2026.4.29, so its `usage` fields are genuine rather than absent, and cachelens
reports a real 97.0% hit rate from them.

It is committed **redacted** (`cachelens redact`): block boundaries, byte
lengths, `cache_control` placement and `usage` are intact, every prompt string
is same-length filler. That avoids redistributing openclaw's system prompt and
tool schemas, and strips the host name and filesystem paths the runtime section
embeds. Tool names are kept so the report stays readable.

There are two openclaw traces, and they are a matched pair:

| trace | volatile content below the boundary | result |
|---|---|---|
| `openclaw_2026-4-29_live.jsonl.gz` | held static | 97.0% hit rate, 0 breaks |
| `openclaw_2026-4-29_heartbeat.jsonl.gz` | `HEARTBEAT.md` rewritten each turn | 44.3%, 3 breaks of 3 |

Same binary, same config, same code path. The only difference is whether
anything below openclaw's cache boundary marker actually changed. The second
carries the signature from
[openclaw#75300](https://github.com/openclaw/openclaw/issues/75300):
`cache_read` pinned at 14,457 while `cache_write` runs 9,686 / 10,322 / 10,964.
See the top-level README for the full account, including what this does and
does not reproduce.

## Pinned commits

| agent | commit | date |
|---|---|---|
| aider | `5dc9490` | 2026-05-22 |
| browser-use | `50ad446` | 2026-08-25 |
| gptme | `19e0351` | 2026-08-26 |
| SWE-agent | `3ea751c` | 2026-07-16 |
| openclaw | `a448042` (npm 2026.4.29) | 2026-04-29 |

## Reproducing

Each harness needs its agent installed from the pinned commit, in its own
virtualenv (the dependency sets conflict):

```bash
git clone https://github.com/browser-use/browser-use && git -C browser-use checkout 50ad446
python3 -m venv venv-browser-use && venv-browser-use/bin/pip install -e browser-use
venv-browser-use/bin/python harness/capture_browser_use.py traces/browser_use_30.jsonl 30
```

`capture_aider.py` additionally takes a path to a git repository to use as the
target codebase, since aider's repo map is computed from real files.

## Profiling the traces

Traces are gzipped; `cachelens` reads `.gz` directly.

```bash
cachelens examples/real-agents/traces/browser_use_30.jsonl.gz --req-per-day 500
```

Note that `reported cache hit rate` shows `0.0%` for all four: that field comes
from provider `usage` numbers, and these captures never reached a provider.
Every other figure is computed from the request bodies and is unaffected.
