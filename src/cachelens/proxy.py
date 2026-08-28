"""Record live traffic into a trace, by sitting in front of the provider.

The analysis in this package is only as good as the traffic you can capture,
and capturing it is the part people give up on. This is the least invasive
option: point the agent's base URL at this process, and it forwards every
request upstream unchanged while writing a trace to disk.

It never needs credentials of its own. Whatever ``Authorization`` or
``x-api-key`` header the client sent is passed straight through, so the key
stays with the agent you are profiling and never reaches this file.

One trap is worth knowing about before you write your own version of this,
because it fails silently rather than loudly: if you forward the client's
``Accept-Encoding`` header, the provider may gzip the SSE stream. A parser
reading those bytes as text finds no ``message_start`` event, so every usage
counter comes back ``0`` -- which reads as "caching is off" rather than "the
capture is broken". We strip the header on the way out and still decompress
defensively on the way back.
"""
from __future__ import annotations

import gzip
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_UPSTREAM = "https://api.anthropic.com"

# Sent by us, not the client: forwarding these would either break the hop or
# defeat the usage parse below.
_HOP_BY_HOP = frozenset({
    "host", "content-length", "connection", "accept-encoding",
    "transfer-encoding", "keep-alive", "upgrade", "te", "trailer",
})


def usage_from_response(body: bytes) -> dict[str, int]:
    """Pull the usage counters out of a response, streaming or not.

    Anthropic reports input usage on ``message_start`` and output usage on
    ``message_delta``, so a streamed response needs both events merged.
    """
    if body[:2] == b"\x1f\x8b":  # gzip magic, in case upstream compressed anyway
        try:
            body = gzip.decompress(body)
        except Exception:
            return {}

    text = body.decode("utf-8", "replace")
    usage: dict[str, int] = {}

    def merge(u: Any) -> None:
        if isinstance(u, dict):
            for k, v in u.items():
                if isinstance(v, int):
                    usage[k] = v

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            merge(json.loads(stripped).get("usage"))
        except Exception:
            pass
        return usage

    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except Exception:
            continue
        merge(event.get("usage"))
        message = event.get("message")
        if isinstance(message, dict):
            merge(message.get("usage"))
    return usage


class _Recorder:
    """Serializes trace writes; the server is threaded."""

    def __init__(self, out_path: Path, session_id: str, quiet: bool = False):
        self.path = out_path
        self.session_id = session_id
        self.quiet = quiet
        self.count = 0
        self._lock = threading.Lock()

    def write(self, payload: dict, usage: dict[str, int], ts: float) -> None:
        with self._lock:
            record = {
                "session_id": self.session_id,
                "request_id": payload.get("_request_id") or f"req_{self.count:04d}",
                "ts": ts,
                "model": payload.get("model", ""),
                "tools": payload.get("tools") or [],
                "system": payload.get("system") or [],
                "messages": payload.get("messages") or [],
                "usage": usage,
            }
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.count += 1
            if not self.quiet:
                n_sys = len(record["system"]) if isinstance(record["system"], list) else 1
                print(
                    f"[rec {self.count - 1:04d}] {record['model']}  "
                    f"tools={len(record['tools'])} sys={n_sys} "
                    f"msgs={len(record['messages'])} "
                    f"write={usage.get('cache_creation_input_tokens', 0)} "
                    f"read={usage.get('cache_read_input_tokens', 0)}",
                    file=sys.stderr, flush=True,
                )


def _handler_class(recorder: _Recorder, upstream: str, forward: bool):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # noqa: D102 - quiet the default access log
            pass

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length)
            ts = time.time()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}

            if not forward:
                # Offline mode: record the request and hand back a stub so an
                # agent can be driven without spending anything.
                recorder.write(payload, {}, ts)
                self._respond(200, json.dumps({
                    "id": "msg_capture", "type": "message", "role": "assistant",
                    "model": payload.get("model", ""),
                    "content": [{"type": "text", "text": "[cachelens proxy: not forwarded]"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }).encode())
                return

            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP}
            req = urllib.request.Request(
                upstream.rstrip("/") + self.path, data=raw, headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    body, status = resp.read(), resp.status
                    out_headers = [
                        (k, v) for k, v in resp.headers.items()
                        if k.lower() not in _HOP_BY_HOP
                    ]
            except urllib.error.HTTPError as err:
                body, status, out_headers = err.read(), err.code, []
            except Exception as err:  # upstream unreachable
                recorder.write(payload, {}, ts)
                self._respond(502, json.dumps({
                    "type": "error",
                    "error": {"type": "api_error", "message": f"proxy: {err}"},
                }).encode())
                return

            recorder.write(payload, usage_from_response(body), ts)
            self._respond(status, body, out_headers)

        def _respond(self, status: int, body: bytes, extra=()):
            self.send_response(status)
            sent = set()
            for key, value in extra:
                if key.lower() == "content-length":
                    continue
                self.send_header(key, value)
                sent.add(key.lower())
            if "content-type" not in sent:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(
    out: str | Path,
    port: int = 8788,
    upstream: str = DEFAULT_UPSTREAM,
    forward: bool = True,
    session_id: str = "proxy-capture",
    quiet: bool = False,
) -> None:
    """Run the recording proxy until interrupted."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    recorder = _Recorder(out_path, session_id, quiet)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), _handler_class(recorder, upstream, forward)
    )
    mode = f"forwarding to {upstream}" if forward else "NOT forwarding (offline capture)"
    print(f"cachelens proxy on http://127.0.0.1:{port} -> {out_path}  [{mode}]",
          file=sys.stderr, flush=True)
    print(f"point your agent's base URL at http://127.0.0.1:{port}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(f"\ncaptured {recorder.count} requests -> {out_path}",
              file=sys.stderr, flush=True)
