"""cachelens - a profiler for LLM prompt-cache economics.

Dashboards tell you the cache-hit rate. This tells you which bytes broke the
prefix, what that cost, and what to change.
"""
from .analyze import analyze, analyze_session
from .ingest import load_jsonl

__version__ = "0.1.0"
__all__ = ["analyze", "analyze_session", "load_jsonl"]
