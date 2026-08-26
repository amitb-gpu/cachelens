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
