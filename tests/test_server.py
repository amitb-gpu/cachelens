"""The read-only HTTP layer and the shapes the WebMCP tools consume."""
import json
import pathlib
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cachelens.analyze import analyze
from cachelens.ingest import load_jsonl
from cachelens.serialize import (
    TOOL_OUTPUT_BUDGET,
    estimate_split_dict,
    explain_dict,
    grouped_causes,
    report_dict,
    summary_dict,
)
from cachelens.server import Catalog, _handler_class, build_payload, default_catalog

TRACES = pathlib.Path(__file__).resolve().parents[1] / "examples" / "real-agents" / "traces"
pytestmark = pytest.mark.skipif(not TRACES.exists(), reason="bundled traces absent")


@pytest.fixture(scope="module")
def catalog():
    return default_catalog(TRACES)


def _rep(name):
    return analyze(load_jsonl(TRACES / name))[0]


# ---- serialization is projection, not recomputation ------------------------

def test_serialized_numbers_match_the_report_objects():
    rep = _rep("openclaw_2026-4-29_heartbeat.jsonl.gz")
    s = summary_dict(rep)
    assert s["turns"] == rep.turns
    assert s["breaks"] == len(rep.actual_breaks)
    assert s["avoidable_breaks"] == len(rep.avoidable_breaks)
    assert s["stale_tokens"] == rep.total_lost_tokens
    assert s["novel_tokens"] == rep.total_novel_tokens
    assert s["wasted_usd"] == pytest.approx(rep.total_wasted_usd, abs=1e-6)
    assert s["has_provider_usage"] is True, "this trace carries real usage counters"


def test_report_is_json_serializable_end_to_end():
    for name in ("browser_use_30.jsonl.gz", "gptme.jsonl.gz"):
        json.dumps(report_dict(_rep(name)))


def test_causes_group_rather_than_repeat():
    """29 breaks, 2 distinct codes: grouping is the point."""
    rep = _rep("browser_use_30.jsonl.gz")
    groups = grouped_causes(rep)
    assert len(groups) < len(rep.actual_breaks)
    assert sum(g["occurrences"] for g in groups) >= len(rep.actual_breaks)
    assert groups[0]["severity"] in ("critical", "high", "medium", "info")


def test_explain_caps_the_breaks_it_returns():
    rep = _rep("browser_use_30.jsonl.gz")
    body = explain_dict(rep, limit=3)
    assert body["breaks_shown"] == 3
    assert body["breaks_total"] == len(rep.actual_breaks)
    assert body["breaks_total"] > body["breaks_shown"], "must not dump everything"


def test_explain_filters_by_cause_code():
    rep = _rep("browser_use_30.jsonl.gz")
    body = explain_dict(rep, cause_code="breakpoint_on_volatile_block", limit=5)
    assert body["filtered_to_cause"] == "BREAKPOINT_ON_VOLATILE_BLOCK"
    for b in body["representative_breaks"]:
        assert any(c["code"] == "BREAKPOINT_ON_VOLATILE_BLOCK" for c in b["causes"])


# ---- the narrow counterfactual --------------------------------------------

def test_split_estimate_is_the_cost_model_not_a_second_opinion():
    rep = _rep("openclaw_2026-4-29_heartbeat.jsonl.gz")
    brk = rep.actual_breaks[0]
    e = estimate_split_dict(rep, brk.turn)
    assert e["stale_tokens"] == brk.lost_tokens
    assert e["novel_tokens"] == brk.novel_tokens
    # The saving IS the break's wasted_usd; it must not be recomputed differently.
    assert e["saving_usd"] == pytest.approx(brk.wasted_usd, abs=1e-6)
    assert e["current_usd"] > e["if_split_usd"]
    assert "assumption" in e and "not_modelled" in e


def test_split_estimate_refuses_a_turn_that_is_not_a_break():
    rep = _rep("openclaw_2026-4-29_heartbeat.jsonl.gz")
    e = estimate_split_dict(rep, 999)
    assert e["error"] == "no_break_at_turn"
    assert e["breaks_at_turns"], "tell the caller which turns are valid"


def test_clean_trace_offers_no_savings_to_estimate():
    rep = _rep("gptme.jsonl.gz")
    assert rep.actual_breaks == []
    assert estimate_split_dict(rep, 1)["error"] == "no_break_at_turn"


# ---- routing and the allowlist --------------------------------------------

def test_catalog_lists_only_known_ids(catalog):
    assert "openclaw-heartbeat" in catalog.ids
    assert all("/" not in i and ".." not in i for i in catalog.ids)


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "..", "%2e%2e", "nope", "", "openclaw_heartbeat.jsonl.gz",
])
def test_unknown_or_hostile_trace_id_is_refused(catalog, bad):
    status, body = build_payload(catalog, f"/api/traces/{bad}/explain", {})
    assert status == 404
    assert body["error"] in ("unknown_trace_id", "unknown_endpoint")
    assert "path" not in json.dumps(body).lower() or "valid_trace_ids" in body


def test_trace_id_never_resolves_to_a_filesystem_path(catalog):
    """The catalog maps ids to paths; a path must never come back out."""
    status, body = build_payload(catalog, "/api/traces", {})
    assert status == 200
    blob = json.dumps(body)
    assert "/examples/" not in blob and ".jsonl" not in blob


def test_limit_is_clamped(catalog):
    _, body = build_payload(catalog, "/api/traces/browser-use/explain", {"limit": ["9999"]})
    assert body["breaks_shown"] <= 10
    _, body = build_payload(catalog, "/api/traces/browser-use/explain", {"limit": ["-4"]})
    assert body["breaks_shown"] >= 1
    _, body = build_payload(catalog, "/api/traces/browser-use/explain", {"limit": ["abc"]})
    assert body["breaks_shown"] >= 1


def test_estimate_requires_a_real_turn(catalog):
    status, body = build_payload(catalog, "/api/traces/openclaw-heartbeat/estimate",
                                 {"turn": ["999"]})
    assert status == 404 and body["error"] == "no_break_at_turn"


# ---- response size ---------------------------------------------------------

def test_api_payloads_stay_sane(catalog):
    """The page can scroll; these still must not be unbounded."""
    for tid in catalog.ids:
        _, body = build_payload(catalog, f"/api/traces/{tid}/explain", {})
        assert len(json.dumps(body)) < 40_000, f"{tid} explain payload too large"


def test_tool_sized_slice_fits_the_webmcp_budget(catalog):
    """What a tool returns must fit the ~1.5K per-output guidance.

    The browser page renders JSON; the tools render text. This checks the
    inputs to that text are small enough that a terse rendering fits.
    """
    for tid in catalog.ids:
        _, body = build_payload(catalog, f"/api/traces/{tid}/explain", {"limit": ["2"]})
        rendered = (
            json.dumps(body["summary"]["causes"])
            + "".join(c["detail"] + c["fix"] for c in body["causes"])
            + "".join((c["evidence"] or [""])[0]
                      for b in body["representative_breaks"] for c in b["causes"])
        )
        assert len(rendered) < TOOL_OUTPUT_BUDGET * 3, f"{tid} too verbose to summarize"


# ---- over a real socket ----------------------------------------------------

def test_http_round_trip_serves_api_and_page(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>x</title>ok")
    cat = default_catalog(TRACES)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(cat, web))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/api/traces", timeout=10) as r:
            assert r.status == 200
            assert json.loads(r.read())["count"] >= 1
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            assert r.status == 200 and b"ok" in r.read()
        try:
            urllib.request.urlopen(base + "/api/traces/nope/explain", timeout=10)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            assert "valid_trace_ids" in json.loads(e.read())
        # static traversal must not escape web/
        try:
            urllib.request.urlopen(base + "/../../../etc/passwd", timeout=10)
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
