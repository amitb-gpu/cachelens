"""Capture real gptme request payloads without an API key.

Calls gptme's own _prepare_messages_for_api(), the single function that
produces the Anthropic request body (system transform, file handling,
cache_control placement, tool prep). Only the conversation content is
scripted; every structural decision is gptme's.
"""
import json, sys, time

OUT = sys.argv[1]
TURNS = int(sys.argv[2]) if len(sys.argv) > 2 else 10

from gptme.llm.llm_anthropic import _prepare_messages_for_api
from gptme.message import Message
from gptme.tools import init_tools, get_tools
from gptme.prompts import get_prompt

MODEL = "claude-sonnet-4-5"

def main():
    init_tools(allowlist=["shell", "save", "patch", "read"])
    tools = list(get_tools())
    system_msgs = list(get_prompt(tools=tools, interactive=False, model=MODEL))

    convo = list(system_msgs)
    captured = []
    exchanges = [
        ("Read src/cachelens/prefix.py and tell me how divergence is found.",
         "The module keeps a rolling sha256 per block and returns the first index where two block lists stop agreeing."),
        ("Now check how cost.py turns that into dollars.",
         "It multiplies the lost token count by the gap between the write rate and the read rate for the model."),
        ("Run the test suite.",
         "```shell\npytest -q\n```"),
        ("Tests pass. What does classify.py do with the diff spans?",
         "It runs each span through an ordered rule table and returns the first matching cause with a fix suggestion."),
        ("Add a rule for a stray hostname in the prefix.",
         "```patch\nsrc/cachelens/classify.py\n<<<<<<< ORIGINAL\nRULES = [\n=======\nRULES = [\n    HOSTNAME_RULE,\n>>>>>>> UPDATED\n```"),
        ("Explain the terminal report layout.",
         "It prints a header block, a cause histogram, a per-turn timeline, then the detailed break list."),
        ("How is the CI gate implemented?",
         "The CLI takes --fail-on and --max-wasted-usd and returns a nonzero exit code when either threshold trips."),
        ("Summarize what we changed today.",
         "We reviewed the prefix and cost modules, ran the suite, and added a hostname classification rule."),
        ("What should we do next?",
         "Add provider token counting so the reported figures are exact rather than byte-heuristic estimates."),
        ("Good. Write that to TODO.md.",
         "```save TODO.md\n- swap the byte heuristic for provider count_tokens\n```"),
    ]
    t0 = time.time()
    for i, (user, assistant) in enumerate(exchanges[:TURNS]):
        convo.append(Message("user", user))
        msgs, system, tools_dict = _prepare_messages_for_api(convo, tools, model=MODEL)
        captured.append({
            "ts": t0 + i * 20.0,
            "messages": to_jsonable(msgs),
            "system": to_jsonable(system),
            "tools": to_jsonable(tools_dict) or [],
        })
        convo.append(Message("assistant", assistant))

    with open(OUT, "w") as f:
        for i, c in enumerate(captured):
            rec = {
                "session_id": "gptme-real",
                "request_id": f"req_{i:04d}",
                "ts": c["ts"],
                "model": MODEL,
                "tools": c["tools"],
                "system": c["system"],
                "messages": c["messages"],
            }
            f.write(json.dumps(rec) + "\n")
    print(f"captured {len(captured)} requests -> {OUT}", file=sys.stderr)

def to_jsonable(o):
    if o is None: return None
    if hasattr(o, "model_dump"): return o.model_dump(exclude_none=True)
    if isinstance(o, dict): return {k: to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [to_jsonable(v) for v in o]
    return o

main()
