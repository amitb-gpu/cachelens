"""Generate two synthetic traces: one broken, one fixed.

The broken one reproduces the shape of a real bug found in a widely used
open-source agent, where per-message metadata was appended to the system
prompt *before* the cache_control marker, so a ~9.5k-token stable block was
rewritten on every single turn.
"""
import json
import pathlib
import time

HERE = pathlib.Path(__file__).parent

STABLE_SYSTEM = (
    "You are a careful software engineering assistant.\n"
    + "\n".join(f"Guideline {i}: prefer small, reversible changes and explain trade-offs "
                f"before acting. Never edit files outside the workspace root."
                for i in range(1, 121))
)

TOOLS = [
    {
        "name": name,
        "description": f"{desc} Returns a structured result.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path"],
        },
    }
    for name, desc in [
        ("read_file", "Read a file from the workspace."),
        ("write_file", "Write a file to the workspace."),
        ("list_dir", "List a directory."),
        ("run_tests", "Run the test suite."),
        ("search", "Search the codebase."),
    ]
]

T0 = 1_756_200_000.0


def turn(i, broken):
    ts = T0 + i * 45
    system_text = STABLE_SYSTEM
    if broken:
        # The bug: volatile metadata glued onto the stable block, inside the
        # region the breakpoint is supposed to cache.
        system_text += (
            f"\n\n<conversation_info>\nmessage_id: msg_{i:04d}\n"
            f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))}\n"
            "</conversation_info>"
        )
    messages = []
    for k in range(i + 1):
        messages.append({"role": "user", "content": f"Please handle sub-task {k}."})
        if k < i:
            messages.append({"role": "assistant", "content": f"Done with sub-task {k}."})
    return {
        "session_id": "agent-run-1",
        "request_id": f"msg_{i:04d}",
        "ts": ts,
        "model": "claude-sonnet-4-6",
        "tools": TOOLS,
        "system": [
            {"type": "text", "text": system_text,
             "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ],
        "messages": messages,
        "usage": {
            "input_tokens": 40,
            "output_tokens": 120,
            "cache_creation_input_tokens": 9531 if broken else 0,
            "cache_read_input_tokens": 10638 if broken else 20169,
        },
    }


for name, broken in [("openclaw_repro.jsonl", True), ("fixed.jsonl", False)]:
    (HERE / name).write_text(
        "\n".join(json.dumps(turn(i, broken)) for i in range(12)) + "\n",
        encoding="utf-8",
    )
    print("wrote", name)
