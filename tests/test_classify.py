from cachelens.analyze import analyze_session
from helpers import codes, req, tool, STABLE


def test_detects_timestamp_injected_into_system_prompt():
    rep = analyze_session([
        req(0, system=STABLE + "\ntimestamp: 2026-08-26T09:20:00Z"),
        req(1, system=STABLE + "\ntimestamp: 2026-08-26T09:21:00Z"),
    ])
    assert "VOLATILE_TIMESTAMP" in codes(rep)
    assert "MISPLACED_BREAKPOINT" in codes(rep)
    assert rep.total_lost_tokens > 0


def test_detects_object_repr_leak():
    rep = analyze_session([
        req(0, system=STABLE + "\nexample: <Row object at 0x7f9a1c2d3e40>"),
        req(1, system=STABLE + "\nexample: <Row object at 0x7f9a1c2dff10>"),
    ])
    assert "OBJECT_REPR" in codes(rep)


def test_detects_uuid_injection():
    rep = analyze_session([
        req(0, system=STABLE + "\nrun: 3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
        req(1, system=STABLE + "\nrun: 7c9e6679-7425-40de-944b-e07fc1f90ae7"),
    ])
    assert "UUID_INJECTION" in codes(rep)


def test_detects_tool_reordering():
    ts = [tool("alpha"), tool("beta"), tool("gamma")]
    rep = analyze_session([req(0, tools=ts), req(1, tools=[ts[2], ts[0], ts[1]])])
    assert "TOOL_REORDER" in codes(rep)


def test_detects_serialization_drift_without_semantic_change():
    a = {"name": "t", "description": "d", "input_schema": {"a": 1, "b": 2}}
    b = {"name": "t", "input_schema": {"b": 2, "a": 1}, "description": "d"}
    rep = analyze_session([req(0, tools=[a]), req(1, tools=[b])])
    assert "SERIALIZATION_DRIFT" in codes(rep)


SAME = [{"role": "user", "content": "identical every turn"}]


def test_detects_ttl_expiry_on_an_unchanged_prefix():
    rep = analyze_session([req(0, ts=0.0, ttl="5m", messages=SAME),
                           req(1, ts=900.0, ttl="5m", messages=SAME)])
    assert "TTL_EXPIRY" in codes(rep)


def test_no_ttl_finding_when_within_window():
    rep = analyze_session([req(0, ts=0.0, ttl="5m", messages=SAME),
                           req(1, ts=120.0, ttl="5m", messages=SAME)])
    assert "TTL_EXPIRY" not in codes(rep)


def test_turn_counter_in_prompt_is_caught():
    rep = analyze_session([req(0), req(1)])   # default message is "turn {i}"
    assert "TURN_COUNTER" in codes(rep)


def test_detects_model_switch():
    rep = analyze_session([req(0, model="claude-sonnet-4-6"),
                           req(1, model="claude-opus-5")])
    assert "MODEL_SWITCH" in codes(rep)


def test_flags_missing_breakpoint():
    rep = analyze_session([req(0, breakpoint_on_system=False),
                           req(1, breakpoint_on_system=False)])
    assert "NO_BREAKPOINT" in codes(rep)


def test_flags_prefix_below_model_minimum():
    rep = analyze_session([req(0, system="short", model="claude-haiku-4-5"),
                           req(1, system="short", model="claude-haiku-4-5")])
    assert "BELOW_MIN_TOKENS" in codes(rep)


def test_growing_conversation_is_clean():
    msgs = [{"role": "user", "content": "hi"}]
    a = req(0, messages=list(msgs))
    msgs += [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "go"}]
    b = req(1, messages=list(msgs))
    rep = analyze_session([a, b])
    assert not rep.avoidable_breaks
    assert rep.total_wasted_usd == 0.0


def _msg_req(i, body, ts=1000.0):
    """One request whose single message block carries the cache breakpoint."""
    from cachelens.ingest.anthropic import record_from_payload

    return record_from_payload({
        "session_id": "s", "request_id": f"r{i}", "ts": ts, "model": "claude-sonnet-4-5",
        "tools": [],
        "system": [{"type": "text", "text": "SYSTEM " * 400,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}
        ]}],
        "usage": {},
    })


def test_breakpoint_on_half_stable_block_is_flagged():
    """The browser-use shape: history and live state share one cached block.

    No timestamp or counter regex matches here -- the tell is purely
    structural, so the rule must not depend on the text patterns.
    """
    from cachelens.analyze import analyze

    history = "step notes and prior observations. " * 300
    prev = history + "CURRENT PAGE ALPHA " * 200
    curr = history + "CURRENT PAGE BETA " * 200
    rep = analyze([_msg_req(0, prev, 1000.0), _msg_req(1, curr, 1010.0)])[0]
    assert "BREAKPOINT_ON_VOLATILE_BLOCK" in codes(rep)
    assert rep.avoidable_breaks


def test_wholly_new_block_is_not_flagged_as_splittable():
    """If nothing carried over, there is no stable part to split out."""
    from cachelens.analyze import analyze

    rep = analyze([
        _msg_req(0, "alpha bravo charlie delta " * 300, 1000.0),
        _msg_req(1, "zulu yankee xray whiskey " * 300, 1010.0),
    ])[0]
    assert "BREAKPOINT_ON_VOLATILE_BLOCK" not in codes(rep)


def test_large_block_diff_stays_fast():
    """Character diffing is quadratic; real DOM-sized blocks must not hang."""
    import time
    from cachelens.classify import _changed_spans

    a = "\n".join(f"[{i}]<a href='/item/{i}'>Result {i} for the quarterly filing</a>"
                  for i in range(1200))
    b = "\n".join(f"[{i}]<a href='/item/{i + 1200}'>Result {i + 1200} for the quarterly filing</a>"
                  for i in range(1200))
    start = time.time()
    spans = _changed_spans(a, b)
    assert time.time() - start < 5.0
    assert spans


def _tools_req(i, tools, ts):
    from cachelens.ingest.anthropic import record_from_payload

    return record_from_payload({
        "session_id": "s", "request_id": f"r{i}", "ts": ts,
        "model": "claude-sonnet-4-5",
        "tools": tools,
        "system": [{"type": "text", "text": "SYSTEM PROMPT LINE " * 500,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "go"}],
        "usage": {},
    })


def _reordered(tool):
    """Same schema, keys emitted in the opposite order."""
    return {k: tool[k] for k in reversed(list(tool))}


def test_tool_key_reorder_breaks_cache_but_not_the_bill():
    """The compact-vs-pretty result, as a regression test.

    A 30-tool set at 47,714 B compact and 81,800 B pretty-printed both count
    14,781 tokens: the provider re-renders tool schemas before tokenizing.
    So reordering keys is a real cache-invalidation event and a non-event
    for billed tokens, and the two must be reported separately.
    """
    from cachelens.analyze import analyze

    tool = {"name": "search", "description": "Search the corpus " * 40,
            "input_schema": {"type": "object",
                             "properties": {"q": {"type": "string"}}}}
    rep = analyze([
        _tools_req(0, [dict(tool)], 1000.0),
        _tools_req(1, [_reordered(tool)], 1010.0),
    ])[0]
    found = codes(rep)
    assert "SERIALIZATION_DRIFT" in found      # the cache really did miss
    assert "TOOL_TOKENS_UNCHANGED" in found    # the bill really did not change

    brk = rep.actual_breaks[0]
    assert brk.novel_tokens == 0, "re-rendered schema content is never new"
    assert brk.lost_tokens > 0, "but it is still re-written, and that costs"


def test_non_tool_serialization_drift_keeps_the_plain_wording():
    """The carve-out is tools-only; system text really is tokenized as sent."""
    from cachelens.ingest.anthropic import record_from_payload
    from cachelens.analyze import analyze

    def req(i, text, ts):
        return record_from_payload({
            "session_id": "s", "request_id": f"r{i}", "ts": ts,
            "model": "claude-sonnet-4-5", "tools": [],
            "system": [{"type": "text", "text": text,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "go"}], "usage": {},
        })

    rep = analyze([req(0, "A " * 900, 1000.0), req(1, "B " * 900, 1010.0)])[0]
    assert "TOOL_TOKENS_UNCHANGED" not in codes(rep)
