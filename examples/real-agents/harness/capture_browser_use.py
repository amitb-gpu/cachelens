"""Capture real browser-use request payloads without a browser or API key.

Drives browser-use's genuine MessageManager + AgentMessagePrompt +
AnthropicMessageSerializer. Only the BrowserStateSummary is synthetic:
a stand-in for a live page, with a realistic DOM listing that changes each
step the way a real page would. Everything that decides cache placement is
browser-use's own code.
"""
import json, sys, time, asyncio, tempfile

OUT = sys.argv[1]
STEPS = int(sys.argv[2]) if len(sys.argv) > 2 else 12

from browser_use.agent.message_manager.service import MessageManager
from browser_use.agent.prompts import SystemPrompt
from browser_use.agent.views import AgentStepInfo
from browser_use.browser.views import BrowserStateSummary, TabInfo, PageInfo
from browser_use.dom.views import SerializedDOMState
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.anthropic.serializer import AnthropicMessageSerializer


class FakeDOM(SerializedDOMState):
    """Stands in for a serialized live DOM; returns a realistic element listing."""
    def __init__(self, text):
        super().__init__(_root=None, selector_map={})
        object.__setattr__(self, "_text", text)

    def llm_representation(self, include_attributes=None):
        return self._text


import os
ROWS_PER_PAGE = int(os.environ.get("BU_ROWS", "110"))  # ~16KB listing; browser-use caps at 40KB


def dom_for_step(i):
    """A plausible search-results page whose contents shift as the agent scrolls."""
    rows = []
    for k in range(ROWS_PER_PAGE):
        n = i * ROWS_PER_PAGE + k
        rows.append(
            f"[{k+3}]<a href='/item/{n}' class='result-link'>Result {n}: "
            f"annual report section {n} — fiscal disclosures and notes</a>"
        )
    head = (
        "[1]<input type='search' name='q' placeholder='Search filings'/>\n"
        "[2]<button type='submit'>Search</button>\n"
    )
    return head + "\n".join(rows)


def state_for_step(i):
    return BrowserStateSummary(
        dom_state=FakeDOM(dom_for_step(i)),
        url=f"https://example-filings.test/search?q=annual+report&page={i+1}",
        title=f"Search results — page {i+1}",
        tabs=[TabInfo(url=f"https://example-filings.test/search?page={i+1}",
                      title=f"Search results — page {i+1}", target_id="tab-1")],
        screenshot=None,
        page_info=PageInfo(viewport_width=1280, viewport_height=1100,
                           page_width=1280, page_height=4200 + i * 40,
                           scroll_x=0, scroll_y=i * 900,
                           pixels_above=i * 900, pixels_below=3300 - i * 40,
                           pixels_left=0, pixels_right=0),
        pixels_above=i * 900, pixels_below=3300 - i * 40,
    )


def main():
    tmp = tempfile.mkdtemp()
    fs = FileSystem(base_dir=tmp)
    system_message = SystemPrompt(max_actions_per_step=10, use_thinking=True, is_anthropic=True, model_name="claude-sonnet-4-5").get_system_message()
    mm = MessageManager(
        task="Find the total revenue figure in the latest annual report and save it to results.md",
        system_message=system_message,
        file_system=fs,
        use_thinking=True,
        max_history_items=None,
        include_attributes=["title", "type", "name", "role", "value", "placeholder"],
    )
    captured = []
    for i in range(STEPS):
        step_info = AgentStepInfo(step_number=i, max_steps=STEPS)
        mm.create_state_messages(
            browser_state_summary=state_for_step(i),
            model_output=None,
            result=None,
            step_info=step_info,
            use_vision=False,
        )
        msgs = mm.get_messages()
        anth_msgs, system = AnthropicMessageSerializer.serialize_messages(msgs)
        captured.append((anth_msgs, system))
        # record what the agent "did", so history grows the way it really would
        mm.state.agent_history_items.append(
            _history_item(i)
        )
    dump(captured, OUT)
    print(f"captured {len(captured)} requests -> {OUT}", file=sys.stderr)


def _history_item(i):
    from browser_use.agent.message_manager.views import HistoryItem
    return HistoryItem(
        step_number=i + 1,
        evaluation_previous_goal=(
            f"Verdict: partial success. The scroll on page {i+1} rendered a new band of "
            f"result rows, and the page reports more content below, but none of the "
            f"visible rows names a revenue figure yet."
        ),
        memory=(
            f"Reviewed {(i+1)*ROWS_PER_PAGE} result rows across {i+1} pages so far. "
            f"Target is the consolidated annual revenue total. Filings seen so far are "
            f"section summaries rather than the statements themselves; the income "
            f"statement is usually deeper in the document set."
        ),
        next_goal=(
            "Scroll one more viewport, then open the highest-numbered filing link that "
            "mentions 'consolidated statements' and read its revenue line."
        ),
        action_results=(
            f"Scrolled down 900px on page {i+1}. New elements became visible. "
            f"No extraction performed on this step."
        ),
    )


def to_jsonable(o):
    if hasattr(o, "model_dump"):
        return o.model_dump(exclude_none=True)
    if isinstance(o, dict):
        return {k: to_jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [to_jsonable(v) for v in o]
    return o


def dump(captured, path):
    t0 = time.time()
    with open(path, "w") as f:
        for i, (msgs, system) in enumerate(captured):
            sys_blocks = to_jsonable(system)
            if isinstance(sys_blocks, str):
                sys_blocks = [{"type": "text", "text": sys_blocks}]
            rec = {
                "session_id": "browser-use-real",
                "request_id": f"req_{i:04d}",
                "ts": t0 + i * 6.0,
                "model": "claude-sonnet-4-5",
                "tools": [],
                "system": sys_blocks or [],
                "messages": to_jsonable(msgs),
            }
            f.write(json.dumps(rec) + "\n")

main()
