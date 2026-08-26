from __future__ import annotations

import argparse
import json
import sys

from .analyze import analyze
from .ingest import load_jsonl
from .report.terminal import render


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cachelens",
        description="Profile prompt-cache economics from captured LLM traffic.",
    )
    p.add_argument("trace", help="JSONL file of captured requests")
    p.add_argument("--req-per-day", type=float, default=0.0,
                   help="extrapolate wasted spend to a monthly figure")
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    p.add_argument("--max-wasted-usd", type=float, default=None,
                   help="CI gate: exit 1 if wasted spend exceeds this")
    p.add_argument("--fail-on", choices=["critical", "high", "none"], default="none",
                   help="CI gate: exit 1 if a cause at this severity or worse is found")
    args = p.parse_args(argv)

    reports = analyze(load_jsonl(args.trace))

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
