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

## Pinned commits

| agent | commit | date |
|---|---|---|
| aider | `5dc9490` | 2026-05-22 |
| browser-use | `50ad446` | 2026-08-25 |
| gptme | `19e0351` | 2026-08-26 |
| SWE-agent | `3ea751c` | 2026-07-16 |

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
