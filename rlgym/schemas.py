"""Artifact schemas for the RL gym.

Every model here is written to disk and later read back by the grader and the M2 API, so
treat them as a wire format: add fields, don't repurpose them.

The central design point is `AttemptStatus` and how reward derives from it. Reward is NOT
something the agent or the executor decides -- it falls out of the terminal status, with one
deliberate asymmetry: ERRORED yields `None`, not 0. An ERRORED attempt means *we failed to
observe an outcome* (API exhausted, browser crashed, gym unreachable), which is categorically
different from the agent failing. Scoring it 0 would make reward correlate with infrastructure
flakiness and quietly bias any dataset built from these artifacts.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

import pydantic
from pydantic import BaseModel, Field


class AttemptStatus(str, Enum):
    """Lifecycle of a single attempt. The last five are terminal."""

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"

    # Agent finished and submitted an answer; the grader decided which of these applies.
    COMPLETED_CORRECT = "COMPLETED_CORRECT"
    # Also covers "agent stopped without ever calling submit_answer" -- it had the budget
    # and produced nothing, which is genuine agent failure.
    COMPLETED_INCORRECT = "COMPLETED_INCORRECT"

    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
    TIMED_OUT = "TIMED_OUT"

    # Infrastructure failure, not the agent's fault.
    ERRORED = "ERRORED"


TERMINAL_STATUSES = frozenset(
    {
        AttemptStatus.COMPLETED_CORRECT,
        AttemptStatus.COMPLETED_INCORRECT,
        AttemptStatus.MAX_STEPS_EXCEEDED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.ERRORED,
    }
)

# Statuses where the agent actually produced (or failed to produce) an answer, so the
# grader has something to judge. Everything else gets its reward directly from status.
GRADEABLE_STATUSES = frozenset(
    {AttemptStatus.COMPLETED_CORRECT, AttemptStatus.COMPLETED_INCORRECT}
)


def reward_for_status(status: AttemptStatus) -> int | None:
    """Binary RL signal, or None when the outcome was not observed.

    None is not 'zero reward' -- it means this attempt must be excluded from pass@k
    denominators rather than counted as a failure.
    """
    if status is AttemptStatus.COMPLETED_CORRECT:
        return 1
    if status is AttemptStatus.ERRORED:
        return None
    if status in TERMINAL_STATUSES:
        return 0
    return None  # non-terminal: not scored yet


class StopReason(str, Enum):
    """Why the agent loop stopped, independent of whether the answer was right."""

    SUBMITTED = "SUBMITTED"  # called submit_answer
    NO_FUNCTION_CALL = "NO_FUNCTION_CALL"  # model stopped acting without submitting
    MAX_STEPS = "MAX_STEPS"
    TIMEOUT = "TIMEOUT"
    API_ERROR = "API_ERROR"
    HARNESS_ERROR = "HARNESS_ERROR"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"


class Task(BaseModel):
    """One problem definition. Static -- never mutated by a run."""

    id: str
    task: str = Field(description="The natural-language prompt shown to the agent.")
    answer: str = Field(
        description="Expected output as a JSON *string*, exactly as it appears in "
        "tasks.json. Use expected_output() to get the parsed dict."
    )

    # Optional per-task budget overrides.
    max_steps: int | None = None
    timeout_s: int | None = None

    def expected_output(self) -> Any:
        """Parse the stored answer. tasks.json holds it double-encoded."""
        return json.loads(self.answer)


class AgentConfig(BaseModel):
    """Which policy ran, and how it was configured. Part of the attempt's identity."""

    agent_type: str = "gemini_computer_use"
    model: str = "gemini-3.6-flash"

    # Optional extra guidance appended to the task prompt. tasks.json has no hint field,
    # so this is an executor-level ablation knob -- recorded so hinted and unhinted
    # attempts can be grouped separately.
    hint: str | None = None

    temperature: float = 1.0

    # Action names to refuse if the model calls them. EMPTY BY DEFAULT -- nothing is
    # blocked. The agent may use the SQL editor or the query builder, its choice. This
    # exists so a future run can constrain without a code change.
    prohibited_actions: list[str] = Field(default_factory=list)


class Limits(BaseModel):
    """Budgets enforced by the executor, not the agent."""

    max_steps: int = 40
    timeout_s: int = 600


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class StepRecord(BaseModel):
    """One iteration of the loop: what the model thought, what it did, what it saw."""

    index: int
    action_name: str
    action_args: dict[str, Any] = Field(default_factory=dict)

    # The model's reasoning text for this turn, when it emitted any.
    reasoning: str | None = None

    # Path to this step's screenshot, relative to the attempt directory.
    screenshot: str | None = None
    url: str | None = None

    # Set when the domain lock or prohibited_actions refused the action. The refusal is
    # returned to the model as an observation rather than silently dropped.
    refused: bool = False
    refused_reason: str | None = None

    # The model asked for confirmation on a flagged action. Unattended rollouts cannot
    # block on a human (upstream calls input() here), so it is auto-acknowledged and
    # recorded. Surfaced explicitly because auto-approving a safety check is a policy
    # choice, not an implementation detail to bury.
    safety_acknowledged: bool = False
    safety_explanation: str | None = None

    latency_ms: int = 0
    tokens: TokenUsage | None = None


class AttemptArtifact(BaseModel):
    """Everything produced by one attempt. Written to runs/<attempt_id>/artifact.json."""

    attempt_id: str
    task_id: str
    job_id: str | None = None
    attempt_index: int = 0

    agent_config: AgentConfig
    limits: Limits
    gym_url: str

    status: AttemptStatus = AttemptStatus.QUEUED
    stop_reason: StopReason | None = None

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None

    steps: list[StepRecord] = Field(default_factory=list)
    step_count: int = 0

    # What the agent handed back via submit_answer. `raw` is kept verbatim so a parse
    # failure is debuggable rather than lost.
    actual_output: Any | None = None
    actual_output_raw: str | None = None

    # Trailing model text when it stopped without submitting.
    final_text: str | None = None

    error: str | None = None

    video_path: str | None = None
    trace_path: str | None = None

    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    # Strategy logging: did the agent reach for the native SQL editor rather than the
    # query builder? Not a restriction -- just lets pass rates be split by approach.
    used_sql_editor: bool = False

    # Actions the domain lock or prohibited_actions refused, and safety confirmations
    # auto-acknowledged. Both are counted so a run's behaviour is auditable.
    refusals: list[str] = Field(default_factory=list)
    safety_acknowledgements: int = 0

    # Kept for symmetry with the retry knob. Attempt-level retry is off by default:
    # transient 429s are already absorbed by the SDK's internal backoff, and k attempts
    # provide the redundancy.
    retry_count: int = 0

    @property
    def reward(self) -> int | None:
        return reward_for_status(self.status)


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    # The orchestrator itself failed, as distinct from its attempts failing.
    FAILED = "FAILED"


class JobSpec(BaseModel):
    """The request: T tasks x K attempts, and how to run them."""

    job_id: str
    task_ids: list[str] = Field(default_factory=list)
    tasks_file: str | None = None

    # Defaults to 1 so an exploratory job is cheap; pass 3+ for pass@k.
    attempts_per_task: int = 1

    # Bounded by one API key's rate limits, not by RAM. Each attempt is its own
    # subprocess, and the browser runs host-side, so memory stays flat as this grows.
    concurrency: int = 3

    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    limits: Limits = Field(default_factory=Limits)
    gym_url: str = "http://localhost:3000"

    headless: bool = True
    record_video: bool = True

    created_at: datetime | None = None

    @property
    def total_attempts(self) -> int:
        return len(self.task_ids) * self.attempts_per_task


class AttemptSummary(BaseModel):
    """A row in the UI's attempt list. Deliberately small -- the full trajectory stays
    in artifact.json and is fetched only when someone opens one attempt."""

    attempt_id: str
    task_id: str
    attempt_index: int

    status: AttemptStatus
    reward: int | None = None
    verdict: str | None = None
    near_miss: bool = False

    step_count: int = 0
    duration_s: float | None = None
    total_tokens: int = 0
    used_sql_editor: bool = False

    error: str | None = None
    diff: str | None = None

    has_video: bool = False
    screenshot_count: int = 0


class TaskResult(BaseModel):
    """Aggregate over one task's K attempts."""

    task_id: str
    prompt: str | None = None
    expected_output: Any | None = None

    attempts: list[AttemptSummary] = Field(default_factory=list)

    total_attempts: int = 0
    # Attempts whose outcome was actually observed. ERRORED ones are excluded rather
    # than counted as failures -- see reward_for_status.
    valid_attempts: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0

    # pass@k proper: did ANY valid attempt succeed?
    pass_at_k: bool = False
    # Per-attempt success rate over valid attempts only. None when nothing was observed.
    pass_rate: float | None = None

    # True when errors ate some attempts, so pass@k rests on fewer samples than
    # requested. Surfaced so a pass@3 computed on n=1 can't look clean.
    low_confidence: bool = False

    def recompute(self, requested_attempts: int) -> None:
        self.total_attempts = len(self.attempts)
        self.passed = sum(1 for a in self.attempts if a.reward == 1)
        self.failed = sum(1 for a in self.attempts if a.reward == 0)
        self.errored = sum(1 for a in self.attempts if a.reward is None)
        self.valid_attempts = self.passed + self.failed
        self.pass_at_k = self.passed > 0
        self.pass_rate = (
            self.passed / self.valid_attempts if self.valid_attempts else None
        )
        self.low_confidence = self.valid_attempts < requested_attempts


class JobResult(BaseModel):
    """Live state of a job. Rewritten to runs/<job_id>/job.json as attempts finish, so
    the API can poll it and a crashed orchestrator leaves a resumable record."""

    spec: JobSpec
    status: JobStatus = JobStatus.QUEUED

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None

    tasks: list[TaskResult] = Field(default_factory=list)

    total_attempts: int = 0
    completed_attempts: int = 0
    valid_attempts: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0

    # Fraction of TASKS with at least one passing attempt -- the headline number.
    pass_at_k_rate: float | None = None
    # Mean reward over valid attempts only.
    mean_reward: float | None = None

    total_tokens: int = 0
    error: str | None = None

    def recompute(self) -> None:
        for task in self.tasks:
            task.recompute(self.spec.attempts_per_task)

        self.total_attempts = self.spec.total_attempts
        self.completed_attempts = sum(t.total_attempts for t in self.tasks)
        self.passed = sum(t.passed for t in self.tasks)
        self.failed = sum(t.failed for t in self.tasks)
        self.errored = sum(t.errored for t in self.tasks)
        self.valid_attempts = self.passed + self.failed
        self.total_tokens = sum(
            a.total_tokens for t in self.tasks for a in t.attempts
        )

        graded_tasks = [t for t in self.tasks if t.valid_attempts > 0]
        self.pass_at_k_rate = (
            sum(1 for t in graded_tasks if t.pass_at_k) / len(graded_tasks)
            if graded_tasks
            else None
        )
        self.mean_reward = (
            self.passed / self.valid_attempts if self.valid_attempts else None
        )


class EvaluationResult(BaseModel):
    """Grading verdict, stored separately so trajectories can be re-graded later.

    `evaluator_version` is the reason this is a separate record: when the comparison
    rules change, stored attempts can be re-scored without re-running any rollouts, and
    old verdicts remain attributable to the rules that produced them.
    """

    attempt_id: str
    task_id: str

    expected_output: Any | None = None
    actual_output: Any | None = None

    verdict: Literal["pass", "fail", "not_graded"]
    reward: int | None = None

    # Human-readable explanation of a failure, for the UI.
    diff: str | None = None

    # True when the answer was structurally right but numerically or textually just off
    # tolerance -- worth reviewing rather than trusting as a clean agent failure.
    near_miss: bool = False

    evaluated_at: datetime
    evaluator_version: str

    model_config = pydantic.ConfigDict(protected_namespaces=())
