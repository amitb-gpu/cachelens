"""Turn raw provider payloads into ordered Block sequences.

Two serializations are kept per block:
  raw       - bytes as actually sent; this is what the provider prefix-hashes
  canonical - semantically normalized; lets us detect the nastiest class of
              cache bug, where two requests mean the same thing but do not
              serialize the same way (dict ordering, whitespace, tool order).
"""
from __future__ import annotations

import json
from typing import Any

from .model import Block


def raw_json(obj: Any) -> str:
    """Serialize preserving key order and structure exactly as given."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def canon_json(obj: Any) -> str:
    """Serialize with sorted keys so semantically-equal payloads compare equal."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(raw_json(item))
        return "\n".join(parts)
    return raw_json(content)


def blocks_from_request(payload: dict[str, Any]) -> list[Block]:
    """Flatten an Anthropic-shaped request body into wire-order blocks."""
    blocks: list[Block] = []

    for i, tool in enumerate(payload.get("tools") or []):
        cc = tool.get("cache_control") if isinstance(tool, dict) else None
        body = {k: v for k, v in tool.items() if k != "cache_control"}
        blocks.append(
            Block(
                level="tools",
                index=i,
                kind="tool",
                raw=raw_json(body),
                canonical=canon_json(body),
                cache_control=cc,
                label=str(body.get("name", f"tool[{i}]")),
            )
        )

    system = payload.get("system")
    if isinstance(system, str):
        system = [{"type": "text", "text": system}]
    for i, blk in enumerate(system or []):
        cc = blk.get("cache_control")
        body = {k: v for k, v in blk.items() if k != "cache_control"}
        text = body.get("text", raw_json(body))
        blocks.append(
            Block(
                level="system",
                index=i,
                kind=str(body.get("type", "text")),
                raw=text if isinstance(text, str) else raw_json(body),
                canonical=canon_json(body),
                cache_control=cc,
                label=f"system[{i}]",
            )
        )

    for i, msg in enumerate(payload.get("messages") or []):
        cc = None
        content = msg.get("content")
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                cc = last.get("cache_control")
        body = {"role": msg.get("role"), "content": content}
        blocks.append(
            Block(
                level="messages",
                index=i,
                kind="message",
                raw=_text_of(content),
                canonical=canon_json(body),
                cache_control=cc,
                label=f"{msg.get('role', '?')}[{i}]",
            )
        )

    return blocks
