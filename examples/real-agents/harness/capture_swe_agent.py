"""Capture SWE-agent request payloads using its real history processor.

Runs SWE-agent's own CacheControlHistoryProcessor with the settings from the
shipped config/default.yaml (type: cache_control, last_n_messages: 2) over a
realistic agent history. Only the conversation content is scripted.
"""
import json, sys, time, copy

OUT = sys.argv[1]
from sweagent.agent.history_processors import CacheControlHistoryProcessor

MODEL = "claude-sonnet-4-5"
SYSTEM = ("You are a helpful assistant that can interact with a computer to solve tasks.\n"
          "You have access to bash, str_replace_editor and submit. Work step by step, "
          "inspect the repository before editing, and run the tests after every change.\n") * 12

TASK = ("<uploaded_files>/testbed</uploaded_files>\n"
        "I've uploaded a python code repository. Consider the following issue:\n"
        "<issue>\nParsing a timestamp with a trailing 'Z' raises ValueError instead of\n"
        "returning a timezone-aware datetime.\n</issue>\n"
        "Fix the issue and make sure the existing tests still pass.\n") * 4

STEPS = [
    ("bash", "find /testbed -name '*.py' | head -40",
     "\n".join(f"/testbed/pkg/module_{i}.py" for i in range(40))),
    ("bash", "grep -rn 'def parse_timestamp' /testbed/pkg",
     "/testbed/pkg/dates.py:41:def parse_timestamp(value: str) -> datetime:"),
    ("bash", "sed -n '30,70p' /testbed/pkg/dates.py",
     "def parse_timestamp(value: str) -> datetime:\n    return datetime.fromisoformat(value)\n" * 6),
    ("str_replace_editor", "str_replace /testbed/pkg/dates.py",
     "The file /testbed/pkg/dates.py has been edited successfully."),
    ("bash", "python -m pytest tests/test_dates.py -q",
     "3 passed in 0.42s"),
    ("bash", "python -m pytest -q",
     "128 passed, 2 warnings in 11.7s"),
    ("bash", "git -C /testbed diff",
     "diff --git a/pkg/dates.py b/pkg/dates.py\n@@\n-    return datetime.fromisoformat(value)\n+    return datetime.fromisoformat(value.replace('Z', '+00:00'))"),
    ("submit", "submit", "Submitted."),
]

def main():
    proc = CacheControlHistoryProcessor(last_n_messages=2)
    history = [
        {"role": "system", "content": SYSTEM, "message_type": "system_prompt"},
        {"role": "user", "content": TASK, "message_type": "observation"},
    ]
    captured = []
    t0 = time.time()
    for i, (tool, cmd, obs) in enumerate(STEPS):
        processed = proc(copy.deepcopy(history))
        captured.append({"ts": t0 + i * 15.0, "history": copy.deepcopy(processed)})
        history.append({"role": "assistant",
                        "content": f"I'll run this next.\n\n<function={tool}>\n{cmd}\n</function>",
                        "message_type": "action"})
        history.append({"role": "user", "content": f"OBSERVATION:\n{obs}",
                        "message_type": "observation"})

    with open(OUT, "w") as f:
        for i, c in enumerate(captured):
            system, msgs = [], []
            for e in c["history"]:
                content = e["content"]
                parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
                if e["role"] == "system":
                    system.extend(parts)
                else:
                    msgs.append({"role": e["role"], "content": parts})
            f.write(json.dumps({
                "session_id": "swe-agent-real", "request_id": f"req_{i:04d}",
                "ts": c["ts"], "model": MODEL, "tools": [],
                "system": system, "messages": msgs,
            }) + "\n")
    print(f"captured {len(captured)} requests -> {OUT}", file=sys.stderr)

main()
