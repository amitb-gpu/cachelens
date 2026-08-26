from cachelens.prefix import first_divergence, prefix_hashes
from helpers import req


def test_identical_prefix_does_not_diverge():
    a, b = req(0), req(1, messages=[{"role": "user", "content": "turn 0"}])
    assert not first_divergence(a.blocks, b.blocks).diverged


def test_appended_messages_are_not_a_break():
    a = req(0, messages=[{"role": "user", "content": "hi"}])
    b = req(1, messages=[{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "hello"},
                         {"role": "user", "content": "more"}])
    d = first_divergence(a.blocks, b.blocks)
    assert not d.diverged and d.reason == "appended"


def test_divergence_reports_the_first_differing_block():
    a = req(0, tools=[{"name": "x", "description": "d", "input_schema": {}},
                      {"name": "y", "description": "d", "input_schema": {}}])
    b = req(1, tools=[{"name": "x", "description": "d", "input_schema": {}},
                      {"name": "z", "description": "d", "input_schema": {}}])
    d = first_divergence(a.blocks, b.blocks)
    assert d.index == 1 and d.level == "tools"


def test_prefix_hashes_are_cumulative():
    a = req(0)
    h = prefix_hashes(a.blocks)
    assert len(h) == len(a.blocks) and len(set(h)) == len(h)
