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


def test_redaction_preserves_shape_and_removes_content(tmp_path):
    """A shared trace must still diagnose, without carrying the prompts."""
    import gzip
    import json

    from cachelens.analyze import analyze
    from cachelens.ingest import load_jsonl
    from cachelens.redact import redact_trace

    secret = "ACME INTERNAL SYSTEM PROMPT do not share. " * 60
    src = tmp_path / "t.jsonl"
    with src.open("w") as f:
        for i in range(3):
            f.write(json.dumps({
                "session_id": "s", "request_id": f"r{i}", "ts": 1000.0 + i * 10,
                "model": "claude-sonnet-4-5",
                "tools": [{"name": "search", "description": "SECRET TOOL DOC " * 30,
                           "input_schema": {"type": "object", "properties": {}}}],
                "system": [{"type": "text", "text": secret,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": f"private question {i}"}],
                "usage": {"cache_creation_input_tokens": 100 * i,
                          "cache_read_input_tokens": 900},
            }) + "\n")

    dst = tmp_path / "t.redacted.jsonl.gz"
    n, _, _ = redact_trace(src, dst)
    assert n == 3

    body = gzip.decompress(dst.read_bytes()).decode()
    assert "ACME" not in body and "SECRET" not in body
    assert "private question" not in body
    assert "search" in body, "tool names are kept so the report stays readable"
    assert "cache_read_input_tokens" in body, "real usage must survive"

    before = analyze(load_jsonl(src))[0]
    after = analyze(load_jsonl(dst))[0]
    assert after.reported_hit_rate == before.reported_hit_rate
    assert len(after.actual_breaks) == len(before.actual_breaks)
    assert after.total_lost_tokens == before.total_lost_tokens


def test_redaction_preserves_a_growing_common_prefix(tmp_path):
    """Line-wise filler keeps the stale/novel boundary inside a block.

    Whole-block filler shared a long constant pad between different blocks,
    which read as a huge false common suffix.
    """
    from cachelens.redact import filler

    a = "\n".join(f"line {i}" for i in range(50))
    b = a + "\n" + "\n".join(f"line {i}" for i in range(50, 60))
    fa, fb = filler(a), filler(b)
    assert len(fa) == len(a) and len(fb) == len(b)
    assert fb.startswith(fa), "shared history must stay a shared prefix"

    x, y = filler("alpha bravo charlie"), filler("delta echo foxtrot!")
    assert len(x) == len(y)
    common = 0
    while common < len(x) and x[-1 - common] == y[-1 - common]:
        common += 1
    assert common < 4, "different content must not share a long suffix"
