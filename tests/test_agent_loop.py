"""Agent-loop tests, with the model stubbed.

The behaviour under test: a malformed function call from the model is the AGENT's mistake
and must not end the attempt. Observed for real in job-20260816-194239 -- Gemini emitted
`drag_and_drop` without `x`, upstream's handler indexes `action.args["x"]` directly, and the
resulting KeyError was being classified as HARNESS_ERROR. That converted three legitimate
agent failures into NOT_OBSERVED and deleted them from the pass@k denominator.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
from google.genai import types

from rlgym.agents.gemini_cu import SUBMIT_ANSWER, GeminiComputerUseAgent
from rlgym.computer import ActionRefused
from rlgym.schemas import AgentConfig, StopReason


class FakeState:
    screenshot = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    url = "http://localhost:3001/question"


class FakeComputer:
    """Only what the loop touches."""

    def __init__(self, refuse: set[str] | None = None) -> None:
        self.refuse = refuse or set()
        self.used_sql_editor = False
        self.refusals: list[str] = []

    def check_action_allowed(self, name: str) -> None:
        if name in self.refuse:
            raise ActionRefused(f"action {name!r} is not permitted in this run")

    def current_state(self) -> FakeState:
        return FakeState()

    def screen_size(self) -> tuple[int, int]:
        return (1440, 900)


def model_turn(name: str, args: dict, text: str | None = "thinking") -> SimpleNamespace:
    """One canned model response carrying a single function call."""
    parts = [types.Part(function_call=types.FunctionCall(name=name, args=args))]
    if text:
        parts.insert(0, types.Part(text=text))
    candidate = types.Candidate(content=types.Content(role="model", parts=parts))
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None, usage_metadata=None)


def run_with(monkeypatch, tmp_path, turns: list[SimpleNamespace], computer=None, max_steps=10):
    agent = GeminiComputerUseAgent(AgentConfig(), api_key="not-used")
    queue = list(turns)
    monkeypatch.setattr(agent, "get_model_response", lambda *a, **k: queue.pop(0))
    return agent.run(
        prompt="do a thing",
        computer=computer or FakeComputer(),
        max_steps=max_steps,
        screenshot_dir=pathlib.Path(tmp_path) / "steps",
    )


# ----------------------------------------------------------------------------------------
# The regression
# ----------------------------------------------------------------------------------------


def test_malformed_call_does_not_end_the_attempt(monkeypatch, tmp_path) -> None:
    """`drag_and_drop` without `x` -- exactly what happened in production."""
    result = run_with(
        monkeypatch,
        tmp_path,
        [
            model_turn("drag_and_drop", {"destination_x": 500, "destination_y": 500}),
            model_turn(SUBMIT_ANSWER, {"answer_json": '{"avg_rating": 3.15}'}),
        ],
    )

    assert result.stop_reason is StopReason.SUBMITTED, "the attempt should have continued"
    assert result.error is None
    assert result.submitted_answer == {"avg_rating": 3.15}

    bad = result.steps[0]
    assert bad.action_name == "drag_and_drop"
    assert bad.action_error and "KeyError" in bad.action_error
    assert bad.refused is False, "a bad call is the agent's mistake, not our refusal"
    # The offending arguments must survive into the transcript -- previously the exception
    # fired before the step was recorded, so the evidence was lost.
    assert bad.action_args == {"destination_x": 500, "destination_y": 500}


def test_error_text_is_the_raw_exception_with_no_guidance(monkeypatch, tmp_path) -> None:
    """We hand back the exception and nothing else. Recovering from its own malformed call
    is part of what is being measured, so no hint about what was missing."""
    result = run_with(
        monkeypatch,
        tmp_path,
        [
            model_turn("drag_and_drop", {"destination_x": 1, "destination_y": 2}),
            model_turn(SUBMIT_ANSWER, {"answer_json": "{}"}),
        ],
    )
    message = result.steps[0].action_error
    assert message == "KeyError: 'x'"
    for coaching in ("should", "must", "try", "expected", "provide", "missing"):
        assert coaching not in message.lower(), f"leaked guidance: {message}"


def test_repeated_malformed_calls_end_as_max_steps_not_errored(monkeypatch, tmp_path) -> None:
    """An agent that never recovers still terminates -- with reward 0, an honest verdict,
    rather than being excluded from pass@k."""
    result = run_with(
        monkeypatch,
        tmp_path,
        [model_turn("drag_and_drop", {"destination_x": 1, "destination_y": 2})] * 4,
        max_steps=3,
    )
    assert result.stop_reason is StopReason.MAX_STEPS
    assert len(result.steps) == 3
    assert all(s.action_error for s in result.steps)


# ----------------------------------------------------------------------------------------
# The distinction that must not blur
# ----------------------------------------------------------------------------------------


def test_refusal_and_malformed_call_are_recorded_differently(monkeypatch, tmp_path) -> None:
    """A refusal is OUR boundary; a malformed call is the AGENT's mistake. Both continue
    the episode, but they are not the same event."""
    computer = FakeComputer(refuse={"scroll"})
    result = run_with(
        monkeypatch,
        tmp_path,
        [
            model_turn("scroll", {"x": 1, "y": 2, "direction": "down"}),
            model_turn("drag_and_drop", {"destination_x": 1, "destination_y": 2}),
            model_turn(SUBMIT_ANSWER, {"answer_json": "{}"}),
        ],
        computer=computer,
    )
    refused, malformed = result.steps[0], result.steps[1]
    assert refused.refused is True and refused.action_error is None
    assert malformed.action_error is not None and malformed.refused is False
    assert result.stop_reason is StopReason.SUBMITTED


def test_a_dead_browser_is_still_a_harness_error(monkeypatch, tmp_path) -> None:
    """If the screenshot after a failed action also fails, the browser is genuinely gone --
    that IS infrastructure, and must not be blamed on the agent."""

    class DeadComputer(FakeComputer):
        def current_state(self):
            raise RuntimeError("Target page, context or browser has been closed")

    result = run_with(
        monkeypatch,
        tmp_path,
        [model_turn("drag_and_drop", {"destination_x": 1, "destination_y": 2})],
        computer=DeadComputer(),
    )
    assert result.stop_reason is StopReason.HARNESS_ERROR
    assert "browser has been closed" in (result.error or "")


def test_a_valid_call_is_unaffected(monkeypatch, tmp_path) -> None:
    """Sanity: the happy path still submits."""
    result = run_with(
        monkeypatch,
        tmp_path,
        [model_turn(SUBMIT_ANSWER, {"answer_json": '{"x": 1}'})],
    )
    assert result.stop_reason is StopReason.SUBMITTED
    assert result.submitted_answer == {"x": 1}
