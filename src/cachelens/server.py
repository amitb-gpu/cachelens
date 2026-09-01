"""A small read-only HTTP layer, so a browser (and an agent) can ask questions.

Deliberately boring: stdlib only, no framework, no database, no auth, no
writes. Every response is a projection of the same ``analyze()`` the CLI
runs, so the page and the terminal cannot drift apart.

The one security decision worth stating plainly: **a trace id is never a
filesystem path**. Ids resolve through the explicit catalog below and nothing
else, so no input from a web page or an agent can reach an arbitrary file.
An unknown id is a 404 with the valid ids listed, not a guess.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .analyze import analyze
from .ingest import load_jsonl
from .serialize import (
    DEFAULT_BREAK_LIMIT,
    estimate_split_dict,
    explain_dict,
    report_dict,
    summary_dict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_DIR = REPO_ROOT / "examples" / "real-agents" / "traces"
WEB_DIR = REPO_ROOT / "web"

MAX_LIMIT = 10


class Catalog:
    """The allowlist. Ids in, file paths never out to a caller."""

    def __init__(self, entries: dict[str, dict]):
        self._entries = entries
        self._reports: dict[str, object] = {}
        self._lock = threading.Lock()

    @property
    def ids(self) -> list[str]:
        return list(self._entries)

    def meta(self, trace_id: str) -> dict | None:
        return self._entries.get(trace_id)

    def report(self, trace_id: str):
        """Analyze once, then serve from memory. Analysis is deterministic."""
        entry = self._entries.get(trace_id)
        if entry is None:
            return None
        with self._lock:
            if trace_id not in self._reports:
                self._reports[trace_id] = analyze(load_jsonl(entry["path"]))[0]
            return self._reports[trace_id]


def default_catalog(trace_dir: Path = TRACE_DIR) -> Catalog:
    """Bundled traces only. Adding a trace is an explicit act, not a scan."""
    known = {
        "openclaw-heartbeat": (
            "openclaw_2026-4-29_heartbeat.jsonl.gz", "OpenClaw 2026.4.29",
            "Live API capture with genuine provider usage counters; volatile "
            "content below the cache boundary changed every turn.",
        ),
        "openclaw-live": (
            "openclaw_2026-4-29_live.jsonl.gz", "OpenClaw 2026.4.29",
            "Same binary and config, with the volatile content held static. "
            "The control case: nothing changed, so nothing broke.",
        ),
        "browser-use": (
            "browser_use_30.jsonl.gz", "browser-use",
            "30 steps. Task, accumulated history and live page state share a "
            "single cached block.",
        ),
        "aider": (
            "aider.jsonl.gz", "aider",
            "Repo map is re-ranked each turn in the middle of the cached region.",
        ),
        "gptme": (
            "gptme.jsonl.gz", "gptme", "Clean run: no prefix breaks.",
        ),
        "swe-agent": (
            "swe_agent.jsonl.gz", "SWE-agent", "Clean run: no prefix breaks.",
        ),
    }
    entries = {}
    for tid, (fname, agent, blurb) in known.items():
        path = trace_dir / fname
        if path.exists():
            entries[tid] = {"path": path, "agent": agent, "blurb": blurb}
    return Catalog(entries)


def _int_param(qs: dict, key: str, default: int) -> int:
    raw = (qs.get(key) or [None])[0]
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def build_payload(catalog: Catalog, path: str, qs: dict) -> tuple[int, dict]:
    """Route a read-only API request. Returns (status, body)."""
    parts = [p for p in path.strip("/").split("/") if p]

    if parts == ["api", "traces"]:
        rows = []
        for tid in catalog.ids:
            rep = catalog.report(tid)
            meta = catalog.meta(tid)
            s = summary_dict(rep)
            rows.append({
                "trace_id": tid,
                "agent": meta["agent"],
                "description": meta["blurb"],
                "session_id": s["session_id"],
                "model": s["model"],
                "turns": s["turns"],
                "reported_hit_rate": s["reported_hit_rate"],
                "has_provider_usage": s["has_provider_usage"],
                "breaks": s["breaks"],
                "avoidable_breaks": s["avoidable_breaks"],
                "wasted_usd": s["wasted_usd"],
            })
        return 200, {"traces": rows, "count": len(rows)}

    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "traces":
        trace_id = parts[2]
        rep = catalog.report(trace_id)
        if rep is None:
            return 404, {
                "error": "unknown_trace_id",
                "message": f"no trace with id {trace_id!r}",
                "valid_trace_ids": catalog.ids,
            }
        tail = parts[3] if len(parts) > 3 else ""

        if tail == "":
            rpd = _int_param(qs, "req_per_day", 0)
            return 200, report_dict(rep, rpd)

        if tail == "explain":
            cause = (qs.get("cause") or [None])[0]
            limit = max(1, min(MAX_LIMIT, _int_param(qs, "limit", DEFAULT_BREAK_LIMIT)))
            body = explain_dict(rep, cause, limit)
            body["trace_id"] = trace_id
            return 200, body

        if tail == "estimate":
            turn = _int_param(qs, "turn", -1)
            body = estimate_split_dict(rep, turn)
            body["trace_id"] = trace_id
            status = 404 if body.get("error") else 200
            return status, body

        return 404, {"error": "unknown_endpoint", "path": path}

    return 404, {"error": "unknown_endpoint", "path": path}


def _handler_class(catalog: Catalog, web_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path.startswith("/api/"):
                try:
                    status, body = build_payload(catalog, parsed.path, qs)
                except Exception as err:  # never leak a traceback to a caller
                    status, body = 500, {"error": "analysis_failed", "message": str(err)}
                self._json(status, body)
                return
            self._static(parsed.path)

        def _json(self, status: int, body: dict):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _static(self, path: str):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (web_dir / rel).resolve()
            # Containment check: a crafted path must not escape web/.
            if not str(target).startswith(str(web_dir.resolve())) or not target.is_file():
                self._json(404, {"error": "not_found", "path": path})
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def serve(port: int = 8000, host: str = "127.0.0.1",
          trace_dir: Path = TRACE_DIR, web_dir: Path = WEB_DIR) -> None:
    catalog = default_catalog(trace_dir)
    if not catalog.ids:
        print(f"cachelens serve: no traces found in {trace_dir}", file=sys.stderr)
    server = ThreadingHTTPServer((host, port), _handler_class(catalog, web_dir))
    print(f"cachelens serve on http://{host}:{port}  "
          f"({len(catalog.ids)} traces)", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
