"""Gemini computer-use policy.

Subclasses the vendored `BrowserAgent` to reuse `handle_action()` and `denormalize_x/y()` --
that action mapping is long, correct, and version-aware (the modern and legacy tool sets have
genuinely different signatures). Everything else is replaced, for four reasons:

1. **Upstream swallows API failures.** `agent.py` wraps the model call in
   `except Exception: return "COMPLETE"`, so once its internal retries are exhausted a
   Gemini 5xx is indistinguishable from the agent finishing its task. Downstream that becomes
   COMPLETED_INCORRECT -> reward 0, which makes reward correlate with infrastructure
   flakiness. Here an exhausted call returns `StopReason.API_ERROR` and the executor maps it
   to ERRORED (reward `None`, excluded from pass@k).

2. **No step cap.** Upstream loops `while status == "CONTINUE"` forever.

3. **The safety prompt blocks on `input()`.** Unattended rollouts would hang.

4. **No per-step instrumentation.** We need screenshots, reasoning, latency and token usage
   recorded per action.

`__init__` deliberately does NOT call `super().__init__()`. Upstream builds its client as
`genai.Client(api_key=..., project=..., location=...)` unconditionally, and google-genai
rejects api_key alongside project/location -- so calling it would reintroduce the exact bug
we hit in Phase 2.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Callable

from .. import vendor  # noqa: F401  -- puts the vendored clone on sys.path
from agent import (  # type: ignore[import-not-found]
    LEGACY_COMPUTER_USE_MODELS,
    LEGACY_PREDEFINED_COMPUTER_USE_FUNCTIONS,
    MAX_RECENT_TURN_WITH_SCREENSHOTS,
    PREDEFINED_COMPUTER_USE_FUNCTIONS,
    BrowserAgent,
)
from google import genai
from google.genai import types
from google.genai.types import Content, FinishReason, FunctionResponse, Part

from ..computer import ActionRefused
from ..schemas import AgentConfig, StepRecord, StopReason, TokenUsage
from .base import AgentRunResult

SUBMIT_ANSWER = "submit_answer"

# Appended to every task prompt. The agent has to be told the answer channel exists, and
# that the episode ends when it uses it.
SUBMIT_INSTRUCTION = f"""
When you have determined the answer, call the `{SUBMIT_ANSWER}` function with a single
argument `answer_json` containing ONLY a JSON object in exactly the format the task asked
for. Do not wrap it in markdown fences or add commentary.

Calling `{SUBMIT_ANSWER}` ends the task, so call it exactly once, and only after you have
actually read the values off the screen. If you cannot determine the answer, submit your
best attempt rather than stopping without submitting.
""".strip()

# Names carrying a screenshot payload, used when pruning old images from history.
_SCREENSHOT_FUNCTIONS = frozenset(
    PREDEFINED_COMPUTER_USE_FUNCTIONS + LEGACY_PREDEFINED_COMPUTER_USE_FUNCTIONS
)

# A malformed function call is retried by re-asking, but a model stuck emitting garbage
# would otherwise spin without consuming step budget.
MAX_CONSECUTIVE_MALFORMED = 3

# When this few steps remain, every observation carries an explicit instruction to submit.
#
# Motivated by an observed failure rather than invented: on problem10 the agent had the
# correct value on screen at step 12, then spent the remaining 28 steps re-running queries
# trying to decide whether Metabase's displayed 3.15 was rounded or exact -- and hit the step
# cap without ever submitting. It had no way to know its budget was running out. Reporting
# remaining budget is standard practice in agent harnesses; without it the model is being
# asked to manage a resource it cannot observe.
SUBMIT_REMINDER_THRESHOLD = 8


def _submit_answer_declaration() -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=SUBMIT_ANSWER,
        description=(
            "Submit the final answer for the task and end the episode. Provide the answer "
            "as a JSON object string matching the format requested in the task."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "answer_json": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "The final answer as a JSON object, e.g. "
                        '{"product_titles": ["A", "B"], "ratings": [4.6, 5]}'
                    ),
                )
            },
            required=["answer_json"],
        ),
    )


class GeminiComputerUseAgent(BrowserAgent):
    def __init__(self, config: AgentConfig, api_key: str) -> None:
        # NOTE: intentionally not calling super().__init__() -- see module docstring.
        self._config = config
        self._model_name = config.model
        self._verbose = False
        self._browser_computer: Any = None
        self._contents: list[Content] = []

        # api_key ONLY. Passing project/location alongside it raises
        # "Project/location and API key are mutually exclusive".
        self._client = genai.Client(api_key=api_key)

        self._use_legacy_computer_use_function_call = (
            config.model in LEGACY_COMPUTER_USE_MODELS
        )

        self._generate_content_config = types.GenerateContentConfig(
            temperature=config.temperature,
            top_p=0.95,
            top_k=40,
            max_output_tokens=8192,
            tools=[
                types.Tool(
                    computer_use=types.ComputerUse(
                        environment=types.Environment.ENVIRONMENT_BROWSER,
                        # Nothing excluded: the agent may attempt any action. The domain
                        # lock refuses off-origin navigation as an environment invariant
                        # rather than by removing tools from its action space.
                        excluded_predefined_functions=[],
                    ),
                ),
                types.Tool(function_declarations=[_submit_answer_declaration()]),
            ],
            thinking_config=types.ThinkingConfig(include_thoughts=True),
        )

    # -- action dispatch ----------------------------------------------------------------

    def handle_action(self, action: types.FunctionCall, use_legacy_actions: bool):
        """Enforce prohibited_actions, then delegate to upstream's mapping."""
        self._browser_computer.check_action_allowed(action.name)
        return super().handle_action(action, use_legacy_actions)

    # -- the loop -----------------------------------------------------------------------

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
        self._browser_computer = computer
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        self._contents = [
            Content(
                role="user",
                parts=[Part(text=f"{prompt}\n\n{SUBMIT_INSTRUCTION}")],
            )
        ]

        steps: list[StepRecord] = []
        totals = TokenUsage()
        acknowledgements = 0
        malformed_streak = 0

        def finish(
            stop_reason: StopReason,
            *,
            submitted: Any | None = None,
            submitted_raw: str | None = None,
            final_text: str | None = None,
            error: str | None = None,
        ) -> AgentRunResult:
            return AgentRunResult(
                stop_reason=stop_reason,
                submitted_answer=submitted,
                submitted_answer_raw=submitted_raw,
                final_text=final_text,
                steps=steps,
                token_usage=totals,
                error=error,
                safety_acknowledgements=acknowledgements,
            )

        while len(steps) < max_steps:
            # Checked here rather than relying on the executor's SIGKILL, so the context
            # closes cleanly and the video is actually written.
            if deadline is not None and time.monotonic() >= deadline:
                return finish(StopReason.TIMEOUT)

            started = time.monotonic()
            try:
                # Upstream's get_model_response already retries 5x with exponential
                # backoff -- the right layer for transient 429s -- and RAISES after
                # exhaustion. We let that propagate instead of swallowing it.
                response = self.get_model_response()
            except Exception as exc:  # noqa: BLE001 -- classified, not hidden
                return finish(
                    StopReason.API_ERROR, error=f"{type(exc).__name__}: {exc}"
                )
            latency_ms = int((time.monotonic() - started) * 1000)

            if response is None or not response.candidates:
                feedback = getattr(response, "prompt_feedback", None)
                if feedback and feedback.block_reason == types.BlockReason.SAFETY:
                    return finish(
                        StopReason.SAFETY_BLOCKED,
                        error=f"prompt blocked for safety: {feedback}",
                    )
                return finish(
                    StopReason.API_ERROR, error="model returned no candidates"
                )

            usage = getattr(response, "usage_metadata", None)
            turn_tokens = TokenUsage(
                prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                total_tokens=getattr(usage, "total_token_count", 0) or 0,
            )
            totals = totals + turn_tokens

            candidate = response.candidates[0]
            if candidate.content:
                self._contents.append(candidate.content)

            reasoning = self.get_text(candidate)
            calls = self.extract_function_calls(candidate)

            if not calls:
                if candidate.finish_reason == FinishReason.MALFORMED_FUNCTION_CALL:
                    malformed_streak += 1
                    if malformed_streak >= MAX_CONSECUTIVE_MALFORMED:
                        return finish(
                            StopReason.API_ERROR,
                            error=f"{malformed_streak} consecutive malformed function calls",
                        )
                    continue
                # The model stopped acting without submitting. That's genuine agent
                # failure, not an error -- it had budget and produced no answer.
                return finish(StopReason.NO_FUNCTION_CALL, final_text=reasoning)
            malformed_streak = 0

            function_responses: list[FunctionResponse] = []

            for call in calls:
                if len(steps) >= max_steps:
                    break

                args = dict(call.args or {})

                # --- the answer channel: terminates the episode -----------------------
                if call.name == SUBMIT_ANSWER:
                    raw = args.get("answer_json")
                    raw_text = raw if isinstance(raw, str) else json.dumps(raw)
                    parsed: Any | None
                    try:
                        parsed = json.loads(raw_text)
                    except (TypeError, ValueError):
                        parsed = None
                    steps.append(
                        StepRecord(
                            index=len(steps),
                            action_name=SUBMIT_ANSWER,
                            action_args={"answer_json": raw_text},
                            reasoning=reasoning,
                            latency_ms=latency_ms,
                            tokens=turn_tokens,
                        )
                    )
                    if on_step:
                        on_step(steps[-1])
                    return finish(
                        StopReason.SUBMITTED,
                        submitted=parsed,
                        submitted_raw=raw_text,
                        final_text=reasoning,
                    )

                # --- safety confirmation, handled without blocking --------------------
                extra_fields: dict[str, str] = {}
                safety = args.get("safety_decision")
                acknowledged = False
                explanation = None
                if isinstance(safety, dict) and safety.get("decision") == (
                    "require_confirmation"
                ):
                    # Upstream calls input() here, which would hang a batch run. Auto-
                    # acknowledged and recorded -- this is a local, read-only gym.
                    acknowledged = True
                    acknowledgements += 1
                    explanation = str(safety.get("explanation") or "")
                    extra_fields["safety_acknowledgement"] = "true"

                # --- execute ----------------------------------------------------------
                refused = False
                refused_reason = None
                action_started = time.monotonic()
                try:
                    result = self.handle_action(
                        call, self._use_legacy_computer_use_function_call
                    )
                except ActionRefused as exc:
                    refused = True
                    refused_reason = exc.reason
                    result = computer.current_state()
                except Exception as exc:  # noqa: BLE001
                    return finish(
                        StopReason.HARNESS_ERROR,
                        error=f"executing {call.name}: {type(exc).__name__}: {exc}",
                    )
                action_ms = int((time.monotonic() - action_started) * 1000)

                screenshot_rel = None
                if hasattr(result, "screenshot"):
                    name = f"step_{len(steps):03d}_{call.name}.png"
                    (screenshot_dir / name).write_bytes(result.screenshot)
                    screenshot_rel = f"{screenshot_dir.name}/{name}"

                    remaining = max_steps - len(steps) - 1
                    payload = {
                        "url": result.url,
                        "steps_remaining": remaining,
                        **extra_fields,
                    }
                    if remaining <= SUBMIT_REMINDER_THRESHOLD:
                        payload["reminder"] = (
                            f"Only {max(remaining, 0)} step(s) remain before this attempt is "
                            f"cut off and scored zero. Call {SUBMIT_ANSWER} now with your "
                            "best answer, even if you are not fully certain of it."
                        )
                    if refused:
                        # Hand the refusal back as an observation so the model can adapt
                        # rather than silently repeating a no-op.
                        payload["error"] = refused_reason
                    function_responses.append(
                        FunctionResponse(
                            name=call.name,
                            response=payload,
                            parts=[
                                types.FunctionResponsePart(
                                    inline_data=types.FunctionResponseBlob(
                                        mime_type="image/png", data=result.screenshot
                                    )
                                )
                            ],
                        )
                    )
                else:
                    # A custom function returning a plain dict.
                    function_responses.append(
                        FunctionResponse(name=call.name, response=dict(result))
                    )

                steps.append(
                    StepRecord(
                        index=len(steps),
                        action_name=call.name,
                        action_args={
                            k: v for k, v in args.items() if k != "safety_decision"
                        },
                        reasoning=reasoning,
                        screenshot=screenshot_rel,
                        url=getattr(result, "url", None),
                        refused=refused,
                        refused_reason=refused_reason,
                        safety_acknowledged=acknowledged,
                        safety_explanation=explanation,
                        latency_ms=latency_ms + action_ms,
                        tokens=turn_tokens,
                    )
                )
                if on_step:
                    on_step(steps[-1])

                # Reasoning belongs to the turn, not to each call within it.
                reasoning = None

            if function_responses:
                self._contents.append(
                    Content(
                        role="user",
                        parts=[Part(function_response=fr) for fr in function_responses],
                    )
                )
            self._prune_old_screenshots()

        return finish(StopReason.MAX_STEPS)

    # -- history management -------------------------------------------------------------

    def _prune_old_screenshots(self) -> None:
        """Keep images only for the most recent few turns, as upstream does.

        Without this, context grows by a full-page PNG every step and cost climbs
        quadratically over a 40-step episode.
        """
        turns_with_screenshots = 0
        for content in reversed(self._contents):
            if content.role != "user" or not content.parts:
                continue
            has_image = any(
                part.function_response
                and part.function_response.parts
                and part.function_response.name in _SCREENSHOT_FUNCTIONS
                for part in content.parts
            )
            if not has_image:
                continue
            turns_with_screenshots += 1
            if turns_with_screenshots > MAX_RECENT_TURN_WITH_SCREENSHOTS:
                for part in content.parts:
                    if (
                        part.function_response
                        and part.function_response.parts
                        and part.function_response.name in _SCREENSHOT_FUNCTIONS
                    ):
                        part.function_response.parts = None
