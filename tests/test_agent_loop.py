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

from rlgym.agents.gemini_cu import (
    ANSWER_KEY,
    SUBMIT_ANSWER,
    SUBMIT_INSTRUCTION,
    GeminiComputerUseAgent,
)
from rlgym.computer import ActionRefused
from rlgym.schemas import AgentConfig, StopReason


class FakeState:
    screenshot = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    url = "http://localhost:3001/question"


class FakeComputer:
    """Only what the loop touches."""

    def __init__(self, refuse: set[str] | None = None) -> None:
        self.refuse = refuse or set()
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


def build_agent(config: AgentConfig | None = None) -> GeminiComputerUseAgent:
    return GeminiComputerUseAgent(config or AgentConfig(), api_key="not-used")


def payloads_from(agent: GeminiComputerUseAgent) -> list[dict]:
    """What the harness handed back to the model as observations."""
    return [
        dict(part.function_response.response or {})
        for content in agent._contents
        if content.role == "user" and content.parts
        for part in content.parts
        if part.function_response
    ]


def run_with(
    monkeypatch, tmp_path, turns: list[SimpleNamespace], computer=None, max_steps=10, agent=None
):
    agent = agent or build_agent()
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
            model_turn(SUBMIT_ANSWER, {ANSWER_KEY: '{"avg_rating": 3.15}'}),
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
            model_turn(SUBMIT_ANSWER, {ANSWER_KEY: "{}"}),
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
            model_turn(SUBMIT_ANSWER, {ANSWER_KEY: "{}"}),
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
        [model_turn(SUBMIT_ANSWER, {ANSWER_KEY: '{"x": 1}'})],
    )
    assert result.stop_reason is StopReason.SUBMITTED
    assert result.submitted_answer == {"x": 1}


# ----------------------------------------------------------------------------------------
# What we hand the agent, and what we deliberately do not
# ----------------------------------------------------------------------------------------


def test_step_budget_is_reported_by_default(monkeypatch, tmp_path) -> None:
    """An agent asked to finish inside a budget it cannot observe is being measured on a
    guess, so remaining steps are reported by default."""
    agent = build_agent()
    run_with(
        monkeypatch,
        tmp_path,
        [
            model_turn("drag_and_drop", {"destination_x": 1, "destination_y": 2}),
            model_turn(SUBMIT_ANSWER, {ANSWER_KEY: "{}"}),
        ],
        agent=agent,
        max_steps=2,
    )

    first = payloads_from(agent)[0]
    assert first["steps_remaining"] == 1
    # Within the threshold, so the observation also spells out what happens at the cap.
    assert "reminder" in first


def test_step_budget_can_be_turned_off(monkeypatch, tmp_path) -> None:
    """Turning it off measures whether the agent can manage a budget it cannot see -- a
    different question, and the reason this is a recorded field rather than a constant."""
    agent = build_agent(AgentConfig(budget_reminders=False))
    run_with(
        monkeypatch,
        tmp_path,
        [
            model_turn("drag_and_drop", {"destination_x": 1, "destination_y": 2}),
            model_turn(SUBMIT_ANSWER, {ANSWER_KEY: "{}"}),
        ],
        agent=agent,
        max_steps=2,
    )

    observations = payloads_from(agent)
    assert observations, "the agent saw no observations at all"
    for payload in observations:
        assert "steps_remaining" not in payload
        assert "reminder" not in payload


def test_submit_instruction_carries_no_task_guidance() -> None:
    """The agent is told the mechanism and nothing else. Restating the task's answer
    format, or suggesting it guess rather than give up, would be us doing part of the
    task."""
    text = SUBMIT_INSTRUCTION.lower()
    for coaching in ("format", "best", "markdown", "commentary", "off the screen"):
        assert coaching not in text, f"leaked guidance: {coaching!r}"


def test_a_non_json_answer_survives_as_itself(monkeypatch, tmp_path) -> None:
    """The agent picks the answer's shape, so a bare string is a legitimate submission.
    It must reach the grader as that string rather than being nulled out for its
    encoding -- 'wrong' and 'badly formatted' are different verdicts."""
    result = run_with(
        monkeypatch, tmp_path, [model_turn(SUBMIT_ANSWER, {ANSWER_KEY: "Q4"})]
    )
    assert result.stop_reason is StopReason.SUBMITTED
    assert result.submitted_answer == "Q4"
    assert result.submitted_answer_raw == "Q4"


def test_the_answer_envelope_reaches_the_grader_intact(monkeypatch, tmp_path) -> None:
    """The loop records what was sent; unwrapping is the grader's business."""
    result = run_with(
        monkeypatch,
        tmp_path,
        [model_turn(SUBMIT_ANSWER, {ANSWER_KEY: '{"answer": {"avg_rating": 3.15}}'})],
    )
    assert result.submitted_answer == {"answer": {"avg_rating": 3.15}}


def test_preamble_states_the_environment_and_nothing_about_the_task() -> None:
    """The opening turn used to be the bare task, so every run spent its first steps
    working out what it was looking at and whether it had to sign in. The preamble states
    the three facts the agent cannot infer and we already know -- and nothing else.
    """
    from rlgym.executor import TASK_PREAMBLE, build_prompt
    from rlgym.tasks import load_task_map

    text = TASK_PREAMBLE.lower()
    # The facts worth stating.
    assert "already loaded" in text and "authenticated" in text

    # Not the product name: recognising the UI is something to read off the page, not to be
    # handed. Not the answer's shape either -- SUBMIT_INSTRUCTION owns that, and choosing it
    # is part of the task.
    for leak in ("metabase", "postgres", "sql", "query builder", "avg", "column", "table"):
        assert leak not in text, f"preamble leaks environment detail: {leak!r}"

    task = load_task_map()["problem1"]
    prompt = build_prompt(task, AgentConfig())
    # The task still arrives verbatim, and after the context rather than buried in it.
    assert task.task in prompt
    assert prompt.index(TASK_PREAMBLE) < prompt.index(task.task)
