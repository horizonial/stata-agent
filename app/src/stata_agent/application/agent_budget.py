"""Validated defaults and bounds for one request-scoped agent loop.

The limits count provider turns and top-level agent tool calls. Executor
internals, such as Stata MCP calls made by one ``run_stata`` tool, retain their
own bounded contracts and do not consume the outer tool-call budget.
"""

from __future__ import annotations

INTERACTIVE_MAX_STEPS_DEFAULT = 16
GOAL_MAX_STEPS_DEFAULT = 64
MAX_TOOL_CALLS_DEFAULT = 128

INTERACTIVE_MAX_STEPS_MIN = 2
INTERACTIVE_MAX_STEPS_MAX = 128
GOAL_MAX_STEPS_MIN = 2
GOAL_MAX_STEPS_MAX = 256
MAX_TOOL_CALLS_MIN = 1
MAX_TOOL_CALLS_MAX = 512


__all__ = [
    "GOAL_MAX_STEPS_DEFAULT",
    "GOAL_MAX_STEPS_MAX",
    "GOAL_MAX_STEPS_MIN",
    "INTERACTIVE_MAX_STEPS_DEFAULT",
    "INTERACTIVE_MAX_STEPS_MAX",
    "INTERACTIVE_MAX_STEPS_MIN",
    "MAX_TOOL_CALLS_DEFAULT",
    "MAX_TOOL_CALLS_MAX",
    "MAX_TOOL_CALLS_MIN",
]
