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
