"""The agent-agnostic seam.

Only one implementation ships (Gemini computer-use), but the executor talks to this
protocol rather than to that class. Swapping in another policy -- a different model, a
scripted baseline, a human-in-the-loop recorder -- means implementing `run()` and nothing
else changes.

An agent is responsible for deciding and taking actions, and for reporting what happened.
It is NOT responsible for deciding whether it succeeded: budgets, status assignment, and
grading all live outside it.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..schemas import StepRecord, StopReason, TokenUsage


class AgentRunResult(BaseModel):
    """Everything the executor needs to build an artifact from one agent run."""

    stop_reason: StopReason

    # The complete first turn as sent to the model: the executor's environment preamble and
    # task, plus whatever contract this agent appended for its own tooling. Reported by the
    # agent because only the agent knows the finished text -- and recorded because "what were
    # we actually asking?" is the first question of any transcript review.
    #
    # NOT a system prompt: this loop sends it as Content(role="user") and sets no system
    # instruction anywhere.
    initial_prompt: str | None = None

    # Set when the agent called submit_answer. `raw` is kept verbatim so an unparseable
    # submission is debuggable instead of lost.
    submitted_answer: Any | None = None
    submitted_answer_raw: str | None = None

    # Trailing model text when it stopped without submitting.
    final_text: str | None = None

    steps: list[StepRecord] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    # Populated when stop_reason is API_ERROR or HARNESS_ERROR.
    error: str | None = None

    safety_acknowledgements: int = 0


@runtime_checkable
class Agent(Protocol):
    def run(
        self,
        *,
        prompt: str,
        computer: Any,
        max_steps: int,
        screenshot_dir: pathlib.Path,
        on_step: Callable[[StepRecord], None] | None = None,
        deadline: float | None = None,
    ) -> AgentRunResult:
        """Drive `computer` toward `prompt` for at most `max_steps` iterations.

        `deadline` is a `time.monotonic()` value. Honouring it lets the run stop *cleanly*
        so the browser context closes and its video is flushed; the executor's subprocess
        kill is only a backstop for a genuinely hung process.

        Must not raise for ordinary failures -- an exhausted API or a crashed browser is
        reported via `stop_reason` and `error` so the executor can classify it as ERRORED
        rather than as the agent being wrong.
        """
        ...
