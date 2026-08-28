"""The capture path. Its failure mode is a silently wrong trace, not an error."""
import gzip
import json

from cachelens.proxy import usage_from_response


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def test_usage_is_merged_across_streaming_events():
    """Input usage arrives on message_start, output on message_delta."""
    body = _sse(
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 12, "cache_creation_input_tokens": 9531,
            "cache_read_input_tokens": 10638}}},
        {"type": "content_block_delta"},
        {"type": "message_delta", "usage": {"output_tokens": 47}},
    )
    u = usage_from_response(body)
    assert u["cache_creation_input_tokens"] == 9531
    assert u["cache_read_input_tokens"] == 10638
    assert u["output_tokens"] == 47


def test_gzipped_stream_still_yields_usage():
    """The trap this proxy exists to have already stepped on.

    Forwarding the client's Accept-Encoding lets the provider gzip the SSE
    stream. A text parser then finds no message_start, every counter reads
    0, and that looks exactly like "caching is off" rather than "the capture
    is broken" -- so it survives review. We strip the header on the way out
    and decompress defensively anyway.
    """
    raw = _sse({"type": "message_start",
                "message": {"usage": {"cache_read_input_tokens": 4242}}})
    assert usage_from_response(gzip.compress(raw)) == usage_from_response(raw)
    assert usage_from_response(gzip.compress(raw))["cache_read_input_tokens"] == 4242


def test_non_streaming_json_response():
    body = json.dumps({"type": "message", "usage": {
        "input_tokens": 5, "cache_read_input_tokens": 777}}).encode()
    assert usage_from_response(body)["cache_read_input_tokens"] == 777


def test_unparseable_body_yields_no_usage_rather_than_crashing():
    assert usage_from_response(b"") == {}
    assert usage_from_response(b"\x1f\x8b not actually gzip") == {}
    assert usage_from_response(b"data: {broken json\n\n") == {}


def test_accept_encoding_is_never_forwarded():
    """Belt and braces: the header must be dropped, not merely tolerated."""
    from cachelens.proxy import _HOP_BY_HOP

    assert "accept-encoding" in _HOP_BY_HOP
    assert "content-length" in _HOP_BY_HOP


def test_proxy_round_trip_records_an_analyzable_trace(tmp_path):
    """Drive the real server over a real socket, in offline mode."""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from cachelens.analyze import analyze
    from cachelens.ingest import load_jsonl
    from cachelens.proxy import _handler_class, _Recorder

    out = tmp_path / "t.jsonl"
    rec = _Recorder(out, "proxy-test", quiet=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(rec, "", forward=False))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        for i in range(3):
            body = {
                "model": "claude-sonnet-4-5",
                "system": [{"type": "text", "text": "STABLE " * 700,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": f"question {i}"}],
            }
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages",
                data=json.dumps(body).encode(),
                headers={"content-type": "application/json",
                         "x-api-key": "sk-ant-the-clients-own-key"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()

    assert rec.count == 3
    rep = analyze(load_jsonl(out))[0]
    assert rep.turns == 3
    assert rep.model == "claude-sonnet-4-5"
