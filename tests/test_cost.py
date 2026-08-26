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
