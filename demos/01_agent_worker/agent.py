"""Demo 1 agent: read one account's usage CSV and write a report that can be checked.

The worker calls run_agent() once per attempt. The Claude Agent SDK starts the agent as
a child process in this same container, with the attempt's directory as its workspace.
"""

import json
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query,
)

MODEL = "claude-haiku-4-5"

PROMPT = """\
The file usage.csv in the current directory holds one customer's daily product usage.
Its columns are date, product, and units.

Use Python with pandas to compute the figures below. Do not estimate them.

Write report.json with exactly these keys:
- row_count: integer, the number of data rows
- total_units: integer, the sum of units
- top_product: string, the product with the highest total units
- peak_day: string in YYYY-MM-DD form, the date with the highest total units

Then write summary.md with three short sentences for the account manager.
"""


def first_line(text, limit):
    return (str(text).strip().splitlines() or [""])[0][:limit]


async def run_agent(workdir: Path):
    """Run the agent in workdir and yield one small event per step."""
    # 1. The run's limits travel with the assignment. Read them from the task file.
    limits = json.loads((workdir / "task.json").read_text())["limits"]

    # 2. Configure the agent. It works in this attempt's directory with three tools.
    options = ClaudeAgentOptions(
        model=MODEL,
        cwd=workdir,
        system_prompt="You are a careful data analyst working in a terminal.",
        tools=["Read", "Write", "Bash"],
        allowed_tools=["Read", "Write", "Bash"],
        permission_mode="acceptEdits",
        setting_sources=[],
        max_turns=limits["max_turns"],
        max_budget_usd=limits["max_cost_usd"],
        extra_args={"no-session-persistence": None},
    )

    # 3. Run the agent loop. Report each step so the worker can record it.
    async for message in query(prompt=PROMPT, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    yield {"type": "agent_text", "summary": first_line(block.text, 100)}
                elif isinstance(block, ToolUseBlock):
                    first_input = first_line(next(iter(block.input.values()), ""), 90)
                    yield {"type": "tool_call", "summary": f"{block.name}: {first_input}",
                           "tool": block.name, "input": block.input}
        elif isinstance(message, ResultMessage):
            usage = message.usage or {}
            tokens_in = sum(usage.get(key, 0) for key in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            cost = message.total_cost_usd or 0.0
            yield {
                "type": "agent_finished",
                "summary": f"{message.subtype}, {message.num_turns} turns, {tokens_in:,} tokens in, ${cost:.3f}",
                "ok": message.subtype == "success" and not message.is_error,
                "turns": message.num_turns,
                "input_tokens": tokens_in,
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": cost,
            }
