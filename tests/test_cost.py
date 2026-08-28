import pytest
from cachelens.cost import base_rate, min_cacheable, waste_for


def test_waste_multiple_is_write_over_read():
    w = waste_for(10_000, "claude-sonnet-4-6", "5m")
    assert w.multiple == pytest.approx(12.5)      # 1.25x write vs 0.10x read


def test_one_hour_ttl_is_twenty_times_a_read():
    assert waste_for(10_000, "claude-sonnet-4-6", "1h").multiple == pytest.approx(20.0)


def test_wasted_usd_matches_hand_calculation():
    # 9,531 tokens at Sonnet 4.6 ($3/MTok base), 5m write vs read
    w = waste_for(9_531, "claude-sonnet-4-6", "5m")
    assert w.paid_usd == pytest.approx(9_531 / 1e6 * 3.00 * 1.25)
    assert w.ideal_usd == pytest.approx(9_531 / 1e6 * 3.00 * 0.10)
    assert w.wasted_usd == pytest.approx(0.03288, abs=1e-5)


def test_model_lookup_tolerates_dated_suffixes():
    assert base_rate("claude-opus-5-20260101") == 5.00
    assert min_cacheable("claude-haiku-4-5") == 4096
    assert min_cacheable("some-unknown-model") == 1024


def test_novel_tokens_only_lose_the_write_premium():
    """Bytes that are new this turn never had a cache entry to hit.

    The best they could have done is ordinary 1.00x input, so marking them
    cacheable costs 0.25x -- not the 12.5x a stale byte loses.
    """
    w = waste_for(0, "claude-sonnet-4-6", "5m", novel_tokens=10_000)
    assert w.paid_usd == pytest.approx(10_000 / 1e6 * 3.00 * 1.25)
    assert w.ideal_usd == pytest.approx(10_000 / 1e6 * 3.00 * 1.00)
    assert w.multiple == pytest.approx(1.25)


def test_stale_and_novel_are_priced_separately():
    w = waste_for(1_000, "claude-sonnet-4-6", "5m", novel_tokens=1_000)
    expected_ideal = (1_000 * 0.10 + 1_000 * 1.00) / 1e6 * 3.00
    assert w.ideal_usd == pytest.approx(expected_ideal)
    assert w.rewritten_tokens == 2_000


def test_split_stale_novel_finds_the_stable_shoulders():
    from cachelens.cost import split_stale_novel, tokens_from_chars

    prev = "STABLE HEAD " * 100 + "volatile-1" + " STABLE TAIL" * 100
    curr = "STABLE HEAD " * 100 + "volatile-2" + " STABLE TAIL" * 100
    stale, novel = split_stale_novel(prev, curr)
    assert novel <= tokens_from_chars(len("volatile-2")) + 1
    assert stale > 500


def test_exact_token_counting_is_strictly_opt_in(monkeypatch):
    """A key in the environment must never make a run go to the network.

    CI is the reason this is strict rather than convenient. A fork whose
    environment exports ANTHROPIC_API_KEY for unrelated purposes would
    otherwise turn a plain `cachelens trace.jsonl` in its pipeline into a
    live-call run: slower, rate-limitable, and red for a reason that appears
    nowhere in the command line. Only --exact-tokens opts in.
    """
    from cachelens.tokens import ExactCounter, HeuristicCounter, get_counter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    assert isinstance(get_counter(), HeuristicCounter)
    assert isinstance(get_counter(None), HeuristicCounter)
    assert isinstance(get_counter(False), HeuristicCounter)
    assert isinstance(get_counter(True), ExactCounter)

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert isinstance(get_counter(True), HeuristicCounter), \
        "explicit --exact-tokens without a key degrades, it does not raise"


def test_cli_default_path_makes_no_network_call(monkeypatch, tmp_path, capsys):
    """The CI gate must run offline even with a key present."""
    import json
    import urllib.request

    from cachelens.cli import main

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    def explode(*a, **k):
        raise AssertionError("cachelens reached the network on its default path")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    asks = ["what does this module do", "explain the rule ordering",
            "summarize the pipeline"]
    trace = tmp_path / "t.jsonl"
    with trace.open("w") as f:
        for i, ask in enumerate(asks):
            f.write(json.dumps({
                "session_id": "s", "request_id": f"r{i}", "ts": 1000.0 + i * 10,
                "model": "claude-sonnet-4-5", "tools": [],
                "system": [{"type": "text", "text": "STABLE SYSTEM PROMPT " * 400,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": ask}],
                "usage": {},
            }) + "\n")

    assert main([str(trace), "--fail-on", "critical"]) == 0
    assert "token counts: heuristic" in capsys.readouterr().out


def test_current_model_rates_are_present_not_defaulted():
    """The dollar column is denominated in these; a miss is not a rounding error.

    Every model here was silently priced at the $3.00 default until this was
    checked: Fable and Mythos are $10.00, so the understatement was 3.3x.
    """
    from cachelens.cost import base_rate, rate_is_known

    expected = {
        "claude-fable-5": 10.00,
        "claude-mythos-5": 10.00,
        "claude-opus-5": 5.00,
        "claude-opus-4-8": 5.00,
        "claude-sonnet-5": 2.00,
        "claude-sonnet-4-6": 3.00,
        "claude-haiku-4-5": 1.00,
    }
    for model, rate in expected.items():
        assert rate_is_known(model), f"{model} falls through to the default rate"
        assert base_rate(model) == rate, f"{model} priced wrong"

    # Dated suffixes must resolve to the same rate, not to the default.
    assert base_rate("claude-opus-5-20260101") == 5.00


def test_unknown_model_is_flagged_rather_than_silently_defaulted():
    from cachelens.analyze import analyze
    from cachelens.cost import rate_is_known
    from helpers import req

    assert not rate_is_known("some-unreleased-model")
    rep = analyze([req(i, model="some-unreleased-model") for i in range(2)])[0]
    assert any("no published rate" in n for n in rep.notes), \
        "a defaulted rate must be visible in the report, not swallowed"

    clean = analyze([req(i, model="claude-sonnet-4-6") for i in range(2)])[0]
    assert not clean.notes
