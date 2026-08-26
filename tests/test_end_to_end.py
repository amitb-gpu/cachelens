import pathlib
from cachelens import analyze, load_jsonl
from cachelens.cli import main

EX = pathlib.Path(__file__).resolve().parents[1] / "examples"


def test_broken_fixture_is_diagnosed():
    rep = analyze(load_jsonl(EX / "openclaw_repro.jsonl"))[0]
    assert rep.actual_breaks, "every turn should break the prefix"
    assert "VOLATILE_TIMESTAMP" in rep.cause_histogram
    assert "MISPLACED_BREAKPOINT" in rep.cause_histogram
    assert rep.total_wasted_usd > 0


def test_fixed_fixture_is_clean():
    rep = analyze(load_jsonl(EX / "fixed.jsonl"))[0]
    assert not rep.actual_breaks
    assert rep.total_wasted_usd == 0.0
    assert rep.reported_hit_rate > 0.95


def test_ci_gate_exit_codes():
    assert main([str(EX / "openclaw_repro.jsonl"), "--json", "--fail-on", "critical"]) == 1
    assert main([str(EX / "fixed.jsonl"), "--json", "--fail-on", "critical"]) == 0
