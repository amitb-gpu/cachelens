from cachelens.ingest.anthropic import record_from_payload

STABLE = "You are a helpful assistant. " * 200


def req(i, *, system=None, tools=None, messages=None, ts=None, model="claude-sonnet-4-6",
        ttl="5m", breakpoint_on_system=True, usage=None):
    sys_block = {"type": "text", "text": system if system is not None else STABLE}
    if breakpoint_on_system:
        sys_block["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    return record_from_payload({
        "session_id": "s",
        "request_id": f"r{i}",
        "ts": ts if ts is not None else 1000.0 + i * 10,
        "model": model,
        "tools": tools or [],
        "system": [sys_block],
        "messages": messages or [{"role": "user", "content": f"turn {i}"}],
        "usage": usage or {},
    })


def tool(name, desc="does a thing"):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": {}}}


def codes(rep):
    return {c.code for b in rep.breaks for c in b.causes}
