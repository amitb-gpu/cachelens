"""Capture real aider request payloads without an API key.

Drives aider's genuine control flow (Coder.run -> format_messages ->
Model.send_completion) against a real git repo, intercepting at
Model.send_completion so the recorded `messages` are byte-for-byte what
aider would have put on the wire. Scripted assistant replies stand in for
the model's output; everything else is aider's own code.
"""
import json, os, sys, time, types

REPO = sys.argv[1]
OUT = sys.argv[2]

os.environ["AIDER_CHECK_UPDATE"] = "0"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-harness-not-used"

from aider.models import Model
from aider.coders import Coder
from aider.io import InputOutput

captured = []

def fake_send_completion(self, messages, functions, stream, temperature=None):
    """Record exactly what aider hands the provider, return a canned reply."""
    captured.append({
        "model": self.name,
        "messages": json.loads(json.dumps(messages)),  # deep copy
        "ts": time.time(),
    })
    from litellm import ModelResponse
    text = SCRIPT[min(len(captured) - 1, len(SCRIPT) - 1)]["reply"]
    resp = ModelResponse(
        choices=[{"message": {"role": "assistant", "content": text},
                  "finish_reason": "stop", "index": 0}],
        model=self.name,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )
    import hashlib
    return hashlib.sha1(b"harness"), resp

SCRIPT = [
    {"user": "What does the prefix module do? Just explain, don't edit.",
     "reply": "The prefix module builds a rolling sha256 over blocks and finds the first index where two requests diverge."},
    {"user": "Explain how cost.py prices a break.",
     "reply": "It multiplies lost tokens by the difference between the write rate and the read rate for the model."},
    {"user": "Add a short docstring to the top of src/cachelens/cost.py.",
     "reply": '''Here is the change:

src/cachelens/cost.py
```python
<<<<<<< SEARCH
"""
=======
"""Pricing for prompt-cache waste."""
"""
>>>>>>> REPLACE
```
'''},
    {"user": "Now explain classify.py's rule ordering.",
     "reply": "Rules are evaluated in priority order and the first match wins, so structural causes outrank textual ones."},
    {"user": "What about the terminal report layout?",
     "reply": "The terminal report prints a summary header, a root-cause histogram, a turn timeline, and then per-turn detail."},
    {"user": "Summarize the whole pipeline in three sentences.",
     "reply": "Requests are canonicalized into ordered blocks. A rolling hash finds the first divergence. The changed span is diffed, classified, and priced."},
    {"user": "Which rule fires for a stray uuid?",
     "reply": "UUID_INJECTION fires when a uuid-shaped token appears before the last breakpoint."},
    {"user": "How is the CI gate wired?",
     "reply": "The CLI exposes --fail-on and --max-wasted-usd, which set a nonzero exit code for the workflow to catch."},
]

def main():
    Model.send_completion = fake_send_completion
    io = InputOutput(yes=True, pretty=False, fancy_input=False, dry_run=False)
    model = Model("claude-sonnet-4-5")
    os.chdir(REPO)
    coder = Coder.create(
        main_model=model,
        io=io,
        fnames=["src/cachelens/prefix.py", "src/cachelens/cost.py"],
        read_only_fnames=["README.md"],
        use_git=True,
        cache_prompts=True,
        stream=False,
        auto_commits=False,
        dirty_commits=False,
        verbose=False,
        map_tokens=1024,
    )
    print("add_cache_headers =", coder.add_cache_headers, file=sys.stderr)
    for step in SCRIPT:
        try:
            coder.run(with_message=step["user"], preproc=False)
        except Exception as e:
            print("turn error:", type(e).__name__, e, file=sys.stderr)
    with open(OUT, "w") as f:
        for i, c in enumerate(captured):
            payload = to_anthropic(c["messages"])
            rec = {
                "session_id": "aider-real",
                "request_id": f"req_{i:04d}",
                "ts": c["ts"],
                "model": "claude-sonnet-4-5",
                "tools": payload["tools"],
                "system": payload["system"],
                "messages": payload["messages"],
            }
            f.write(json.dumps(rec) + "\n")
    print(f"captured {len(captured)} requests -> {OUT}", file=sys.stderr)

def to_anthropic(messages):
    """Map aider's OpenAI-style list onto an Anthropic-shaped body.

    litellm hoists leading system messages into the top-level `system`
    param; trailing system messages (aider's 'reminder') are hoisted too.
    Cache_control markers ride along on the content parts aider set them on.
    """
    system, msgs = [], []
    for m in messages:
        content = m["content"]
        if m["role"] == "system":
            if isinstance(content, str):
                system.append({"type": "text", "text": content})
            else:
                for part in content:
                    system.append(part)
        else:
            msgs.append({"role": m["role"], "content": content})
    return {"tools": [], "system": system, "messages": msgs}

main()
