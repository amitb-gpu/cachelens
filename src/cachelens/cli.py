from __future__ import annotations

import argparse
import json
import sys

from .analyze import analyze
from .ingest import load_jsonl
from .report.terminal import render
from .proxy import DEFAULT_UPSTREAM, serve
from .redact import redact_trace
from .server import serve as serve_http
from .tokens import get_counter


def _serve_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="cachelens serve",
        description="Serve the bundled traces as a read-only browser page and "
                    "JSON API, with WebMCP tools registered for an agent.",
    )
    p.add_argument("--port", type=int, default=8000, help="listen port (default 8000)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1; use 0.0.0.0 to deploy)")
    args = p.parse_args(argv)
    serve_http(port=args.port, host=args.host)
    return 0


def _proxy_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="cachelens proxy",
        description="Record live traffic by sitting in front of the provider. "
                    "Point your agent's base URL at this process; it forwards "
                    "every request upstream unchanged and writes a trace.",
        epilog="It needs no credentials of its own -- the client's auth header "
               "is passed straight through.",
    )
    p.add_argument("-o", "--out", default="trace.jsonl",
                   help="trace file to append to (default: trace.jsonl)")
    p.add_argument("--port", type=int, default=8788, help="listen port (default 8788)")
    p.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                   help=f"provider base URL (default {DEFAULT_UPSTREAM})")
    p.add_argument("--no-forward", dest="forward", action="store_false",
                   help="record requests and return a stub instead of calling "
                        "the provider; captures shape without spending")
    p.add_argument("--session-id", default="proxy-capture",
                   help="session_id to stamp on captured records")
    args = p.parse_args(argv)
    serve(args.out, port=args.port, upstream=args.upstream,
          forward=args.forward, session_id=args.session_id)
    return 0


def _redact_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="cachelens redact",
        description="Write a shape-only copy of a trace: block boundaries, "
                    "byte lengths, cache_control placement and usage are kept; "
                    "all prompt content is replaced by same-length filler.",
    )
    p.add_argument("trace", help="JSONL (or .jsonl.gz) trace to redact")
    p.add_argument("-o", "--out", required=True,
                   help="destination; .gz suffix compresses")
    args = p.parse_args(argv)
    n, b_in, b_out = redact_trace(args.trace, args.out)
    print(f"redacted {n} requests -> {args.out}")
    print(f"  content bytes {b_in:,} -> {b_out:,} (lengths preserved, text replaced)")
    print("  structural findings unchanged; textual rules will no longer match")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "redact":
        return _redact_main(argv[1:])
    if argv and argv[0] == "proxy":
        return _proxy_main(argv[1:])
    if argv and argv[0] == "serve":
        return _serve_main(argv[1:])

    p = argparse.ArgumentParser(
        prog="cachelens",
        description="Profile prompt-cache economics from captured LLM traffic.",
    )
    p.add_argument("trace", help="JSONL file of captured requests")
    p.add_argument("--req-per-day", type=float, default=0.0,
                   help="extrapolate wasted spend to a monthly figure")
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    p.add_argument("--exact-tokens", dest="exact", action="store_true", default=None,
                   help="count tokens via the provider count_tokens endpoint "
                        "(free, needs ANTHROPIC_API_KEY)")
    p.add_argument("--no-exact-tokens", dest="exact", action="store_false",
                   help="force the byte heuristic even if a key is present")
    p.add_argument("--max-wasted-usd", type=float, default=None,
                   help="CI gate: exit 1 if wasted spend exceeds this")
    p.add_argument("--fail-on", choices=["critical", "high", "none"], default="none",
                   help="CI gate: exit 1 if a cause at this severity or worse is found")
    args = p.parse_args(argv)

    counter = get_counter(args.exact)
    reports = analyze(load_jsonl(args.trace), counter)

    if args.json:
        out = [
            {
                "session_id": r.session_id,
                "model": r.model,
                "turns": r.turns,
                "reported_hit_rate": round(r.reported_hit_rate, 4),
                "breaks": len(r.breaks),
                "avoidable_breaks": len(r.avoidable_breaks),
                "lost_tokens": r.total_lost_tokens,
                "wasted_usd": round(r.total_wasted_usd, 6),
                "causes": r.cause_histogram,
                "token_counter": r.counter_name,
                "level_confidence": {
                    lvl: {"basis": c.basis, "error_pct": c.error_pct, "note": c.note}
                    for lvl, c in r.level_confidence.items()
                },
            }
            for r in reports
        ]
        print(json.dumps(out, indent=2))
    else:
        for r in reports:
            print(render(r, args.req_per_day))
            print()

    failed = False
    for r in reports:
        if args.max_wasted_usd is not None and r.total_wasted_usd > args.max_wasted_usd:
            print(f"FAIL {r.session_id}: wasted ${r.total_wasted_usd:.4f} > "
                  f"${args.max_wasted_usd:.4f}", file=sys.stderr)
            failed = True
        if args.fail_on != "none":
            sevs = {"critical": ["critical"], "high": ["critical", "high"]}[args.fail_on]
            hits = [c.code for b in r.breaks for c in b.causes if c.severity in sevs]
            if hits:
                print(f"FAIL {r.session_id}: {len(hits)} finding(s) at severity "
                      f"{args.fail_on} or worse: {sorted(set(hits))}", file=sys.stderr)
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
