"""Artifact schemas for the RL gym.

Every model here is written to disk and later read back by the grader and the M2 API, so
treat them as a wire format: add fields, don't repurpose them.

The central design point is `AttemptStatus` and how reward derives from it. Reward is NOT
something the agent or the executor decides -- it falls out of the terminal status, with one
deliberate asymmetry: NOT_OBSERVED yields `None`, not 0. A NOT_OBSERVED attempt means *we
failed to observe an outcome* (API exhausted, browser crashed, gym would not provision),
which is categorically
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
    """What CLASS of outcome an attempt reached. Drives reward. The last five are terminal.

    See `AttemptPhase` for how far an attempt got, and `StopReason` for why it stopped --
    three separate axes, deliberately not collapsed into one.
    """

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"

    CORRECT = "COMPLETED_CORRECT"
    # An answer exists (or the agent finished without producing one) and is not correct.
    # Named ANSWERED rather than COMPLETED_INCORRECT because the executor writes it *before*
    # grading, when correctness is still unknown -- the grader then promotes it to CORRECT.
    ANSWERED = "ANSWERED"

    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
    TIMED_OUT = "TIMED_OUT"

    # Infrastructure failure: the outcome was never observed, so it is not a loss.
    NOT_OBSERVED = "NOT_OBSERVED"


# Values written by earlier versions and still present in runs/*/artifact.json. Without a
# translation, `read_artifact()` swallows the validation error and returns None, so old
# attempts would silently disappear from list_jobs() and from resume.
_LEGACY_STATUS = {
    "COMPLETED_INCORRECT": AttemptStatus.ANSWERED.value,
    "ERRORED": AttemptStatus.NOT_OBSERVED.value,
}


TERMINAL_STATUSES = frozenset(
    {
        AttemptStatus.CORRECT,
        AttemptStatus.ANSWERED,
        AttemptStatus.MAX_STEPS_EXCEEDED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.NOT_OBSERVED,
    }
)

# Statuses where the agent actually produced (or failed to produce) an answer, so the
# grader has something to judge. Everything else gets its reward directly from status.
GRADEABLE_STATUSES = frozenset({AttemptStatus.CORRECT, AttemptStatus.ANSWERED})


def reward_for_status(status: AttemptStatus) -> int | None:
    """Binary RL signal, or None when the outcome was not observed.

    None is not 'zero reward' -- it means this attempt must be excluded from pass@k
    denominators rather than counted as a failure.
    """
    if status is AttemptStatus.CORRECT:
        return 1
    if status is AttemptStatus.NOT_OBSERVED:
        return None
    if status in TERMINAL_STATUSES:
        return 0
    return None  # non-terminal: not scored yet


class AttemptPhase(str, Enum):
    """How FAR an attempt got. Orthogonal to status, and never drives reward.

    Exists because provisioning a stack makes an attempt invisible for minutes -- the
    artifact used to appear only once the worker started. It also localizes failure:
    "12 attempts errored" becomes "11 died in PROVISIONING, 1 in RUNNING", which is the
    difference between broken infrastructure and a flaky model.
    """

    QUEUED = "QUEUED"              # attempt dir exists, no port claimed yet
    PROVISIONING = "PROVISIONING"  # containers up, dump restoring, Metabase booting
    AUTHENTICATING = "AUTHENTICATING"  # minting and verifying the session
    CONNECTING = "CONNECTING"      # Playwright launch, pre-flight auth check
    RUNNING = "RUNNING"            # the agent loop
    GRADING = "GRADING"
    DONE = "DONE"                  # terminal, whatever the status


class StopReason(str, Enum):
    """WHY an attempt stopped, independent of whether the answer was right."""

    SUBMITTED = "SUBMITTED"  # called submit_answer
    NO_FUNCTION_CALL = "NO_FUNCTION_CALL"  # model stopped acting without submitting
    MAX_STEPS = "MAX_STEPS"
    TIMEOUT = "TIMEOUT"
    API_ERROR = "API_ERROR"
    HARNESS_ERROR = "HARNESS_ERROR"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"

    # Raised in the parent before a worker exists. Two values rather than one because the
    # operator action differs: Docker/disk/image vs. credentials. Deliberately no third
    # value for a browser-launch failure -- `phase` already localizes that, and duplicating
    # localization into this enum is how the two fields drift apart.
    GYM_PROVISION_FAILED = "GYM_PROVISION_FAILED"
    GYM_AUTH_FAILED = "GYM_AUTH_FAILED"


class Task(BaseModel):
    """One problem definition. Static -- never mutated by a run."""

    id: str
    task: str = Field(description="The natural-language prompt shown to the agent.")
    answer: str = Field(
        description="Expected output exactly as it appears in tasks.json -- usually a "
        "JSON *string* (double-encoded), but a bare value is accepted too. Use "
        "expected_output() to get the comparable form."
    )

    # Optional per-task budget overrides.
    max_steps: int | None = None
    timeout_s: int | None = None

    def expected_output(self) -> Any:
        """The answer in comparable form.

        tasks.json holds answers double-encoded, so the normal path is json.loads. An
        answer that is not JSON is returned as the plain string it is, rather than raising:
        a task whose answer is `Q4` is a perfectly gradeable task, and refusing to load the
        file over it would reject a valid task set for a formatting convention we invented.
        """
        try:
            return json.loads(self.answer)
        except (TypeError, ValueError):
            return self.answer


# Models verified in Phase 2 to accept the ComputerUse tool with our API key -- all five
# returned OK. Enumerated rather than fetched from the API, whose model list runs to
# hundreds of entries, almost none of which can drive a browser. Anything not on this list
# can still be passed explicitly; this is what the UI offers, not what the harness allows.
COMPUTER_USE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-computer-use-preview-10-2025",
]


class AgentConfig(BaseModel):
    """Which policy ran, and how it was configured. Part of the attempt's identity."""

    agent_type: str = "gemini_computer_use"
    model: str = "gemini-3.6-flash"

    # Optional extra guidance appended to the task prompt. tasks.json has no hint field,
    # so this is an executor-level ablation knob -- recorded so hinted and unhinted
    # attempts can be grouped separately.
    hint: str | None = None

    # Sampling parameters, part of the attempt's identity: the same model at a different
    # temperature is a different policy, so a job is not comparable to another unless
    # these match. Defaults are the values the harness has always used.
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 40

    # Report the remaining step budget to the agent, and warn it as the cap approaches.
    #
    # ON by default: an agent asked to finish within a budget it cannot observe is being
    # measured on a guess, and reporting remaining steps is standard in agent harnesses.
    # It stays a named field rather than being hardcoded because it materially changes what
    # a run measures -- results with it on and off are not comparable, so which one produced
    # a number has to be recorded alongside the number.
    budget_reminders: bool = True

    # Action names to refuse if the model calls them. EMPTY BY DEFAULT -- nothing is
    # blocked. The agent may use the SQL editor or the query builder, its choice. This
    # exists so a future run can constrain without a code change.
    prohibited_actions: list[str] = Field(default_factory=list)


class Limits(BaseModel):
    """Budgets enforced by the executor, not the agent.

    max_steps is the budget being scored. timeout_s is a backstop for a wedged browser or a
    stalled API, kept loose: TIMED_OUT scores 0, so a tight clock books our own slowness as
    agent failure.
    """

    max_steps: int = 40
    timeout_s: int = 900


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

    # Set when the model emitted a call the tool schema could not satisfy -- a missing or
    # wrong-typed argument. Deliberately distinct from `refused`: a refusal is our boundary,
    # this is the agent's own mistake. Recorded so the malformed call is visible in the
    # transcript instead of vanishing with the exception.
    action_error: str | None = None

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
    phase: AttemptPhase = AttemptPhase.QUEUED
    stop_reason: StopReason | None = None

    # Row counts proving the gym was seeded before this attempt started (see
    # gym_stack._assert_pristine). Evidence rather than assertion.
    pristine_counts: dict[str, int] | None = None

    # Set when the ephemeral stack could not be destroyed. Does NOT change the attempt's
    # status -- the agent's result is still valid -- but a leaked stack keeps its port
    # bound, so the next attempt claiming that port will fail to start. Surfaced here
    # because silent teardown failure is how one bad attempt poisons a whole job.
    teardown_error: str | None = None

    # Set when the host could not be told that the blocks Docker freed are free (see
    # gym_stack.reclaim_host_disk). Same philosophy as teardown_error: reported, never
    # raised, never changes the attempt's status.
    trim_error: str | None = None

    # Host sparse-image footprint vs what the guest filesystem reports as used, sampled
    # after teardown. Recorded as a PAIR because the divergence is the whole diagnostic:
    # two jobs died of host disk exhaustion while the guest sat at 4% used, and one number
    # alone made that invisible for a full job.
    host_image_gib: float | None = None
    guest_used_gib: float | None = None

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None

    steps: list[StepRecord] = Field(default_factory=list)
    step_count: int = 0

    # What the agent handed back via submit_answer. `raw` is kept verbatim so a parse
    # failure is debuggable rather than lost.
    actual_output: Any | None = None
    actual_output_raw: str | None = None

    # The exact first turn the agent was given -- environment preamble, task, and the
    # agent's own answer contract. Stored per attempt so a transcript can be read against
    # what was actually asked, and so a prompt change is visible in the record rather than
    # only in git history.
    initial_prompt: str | None = None

    # Trailing model text when it stopped without submitting.
    final_text: str | None = None

    error: str | None = None

    video_path: str | None = None
    trace_path: str | None = None

    token_usage: TokenUsage = Field(default_factory=TokenUsage)

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

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @pydantic.field_validator("status", mode="before")
    @classmethod
    def _translate_legacy_status(cls, value):
        """Accept status values written before the rename (see _LEGACY_STATUS)."""
        return _LEGACY_STATUS.get(value, value) if isinstance(value, str) else value


class HealthStatus(BaseModel):
    """Whether the platform can actually run anything right now.

    There is no long-lived gym to probe -- each attempt provisions its own. So this checks
    what genuinely gates a run: can we talk to Docker, are both images already pulled (a
    cold pull inside an attempt would look like a hang), is the seed dump there, and are
    the credentials needed to mint a session configured.
    """

    ok: bool = True
    docker_available: bool = False
    images_present: bool = False
    dump_present: bool = False
    credentials_configured: bool = False
    tasks_available: int = 0
    max_gyms: int = 0
    # Human-readable summary of whatever is missing.
    detail: str | None = None

    @property
    def can_run(self) -> bool:
        return (
            self.docker_available
            and self.images_present
            and self.dump_present
            and self.credentials_configured
        )


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

    # Bounded by RAM, not by API rate limits: every attempt holds its own Metabase (~1.1
    # GiB), so ports.MAX_GYMS caps this at 3. This was the opposite while gyms were shared,
    # and the old comment survived the change -- ports.py has said the correct thing since.
    #
    # Defaults to 1 to match every entry point (the API, both CLIs, the console). A library
    # caller used to get 3 here while the CLI gave 1, which is a trap for anyone scripting
    # against service.submit_job().
    concurrency: int = 1

    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    limits: Limits = Field(default_factory=Limits)

    # No gym_url: each attempt provisions its own stack on a port claimed at run time, so
    # there is no job-level URL to configure. AttemptArtifact.gym_url records which one an
    # attempt actually got. Older job.json files still carry the field; pydantic ignores
    # unknown keys, so they keep loading.
    headless: bool = True
    record_video: bool = True

    created_at: datetime | None = None

    @property
    def total_attempts(self) -> int:
        return len(self.task_ids) * self.attempts_per_task


class AttemptSummary(BaseModel):
    """A row in the UI's attempt list. Deliberately small -- the full trajectory stays
    in artifact.json and is fetched only when someone opens one attempt.

    A summary exists for every requested attempt from the moment the job starts, including
    ones that have not run yet: the UI shows the whole T x K grid, so an attempt still in
    the queue needs a row to occupy. Those placeholders carry no `attempt_id` -- there is
    no attempt directory yet -- which is also what tells the UI the row is not openable.
    """

    attempt_id: str = ""
    task_id: str
    attempt_index: int

    status: AttemptStatus = AttemptStatus.QUEUED
    phase: AttemptPhase = AttemptPhase.QUEUED
    reward: int | None = None
    verdict: str | None = None
    near_miss: bool = False

    step_count: int = 0
    duration_s: float | None = None
    total_tokens: int = 0

    error: str | None = None
    diff: str | None = None

    has_video: bool = False
    screenshot_count: int = 0

    @property
    def started(self) -> bool:
        """False for a placeholder -- nothing has been provisioned for it yet."""
        return bool(self.attempt_id)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @pydantic.field_validator("status", mode="before")
    @classmethod
    def _translate_legacy_status(cls, value):
        """job.json is a wire format too -- old summaries must keep loading."""
        return _LEGACY_STATUS.get(value, value) if isinstance(value, str) else value


class RunStats(BaseModel):
    """What a set of attempts cost. Averaged over *observed* attempts only.

    An attempt that died in provisioning contributes 0 steps and a few seconds; including
    those would drag both averages toward zero and make a run look cheaper and faster than
    it is. Only attempts the agent actually ran are counted.
    """

    observed_attempts: int = 0
    total_tokens: int = 0
    total_duration_s: float = 0.0
    avg_duration_s: float | None = None
    avg_steps: float | None = None
    total_steps: int = 0

    @classmethod
    def over(cls, attempts: list[AttemptSummary]) -> RunStats:
        observed = [a for a in attempts if a.is_terminal and a.reward is not None]
        # Tokens are billed whatever the outcome, so they are summed over everything that
        # ran -- unlike the averages, which describe a typical successful rollout.
        tokens = sum(a.total_tokens for a in attempts)
        durations = [a.duration_s for a in observed if a.duration_s is not None]
        steps = [a.step_count for a in observed]
        return cls(
            observed_attempts=len(observed),
            total_tokens=tokens,
            total_duration_s=sum(durations),
            avg_duration_s=sum(durations) / len(durations) if durations else None,
            avg_steps=sum(steps) / len(steps) if steps else None,
            total_steps=sum(steps),
        )


class TaskResult(BaseModel):
    """Aggregate over one task's K attempts."""

    task_id: str
    prompt: str | None = None
    expected_output: Any | None = None

    attempts: list[AttemptSummary] = Field(default_factory=list)

    # How many attempts were REQUESTED (K), not how many exist yet. The attempt list is
    # pre-populated with placeholders, so these two agree by construction -- but the
    # counters below deliberately only look at attempts that reached a terminal state.
    total_attempts: int = 0
    completed: int = 0
    running: int = 0
    queued: int = 0

    # Attempts whose outcome was actually observed. NOT_OBSERVED ones are excluded rather
    # than counted as failures -- see reward_for_status.
    valid_attempts: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0

    # pass@k proper: did ANY valid attempt succeed?
    pass_at_k: bool = False
    # Per-attempt success rate over valid attempts only. None when nothing was observed.
    pass_rate: float | None = None

    stats: RunStats = Field(default_factory=RunStats)

    def recompute(self, requested_attempts: int) -> None:
        self.total_attempts = requested_attempts or len(self.attempts)
        self.running = sum(
            1 for a in self.attempts if a.status is AttemptStatus.IN_PROGRESS
        )
        self.queued = sum(1 for a in self.attempts if a.status is AttemptStatus.QUEUED)

        # Only terminal attempts are scored. Counting a placeholder's `reward is None` as
        # an error would report every unstarted attempt as an infrastructure failure.
        done = [a for a in self.attempts if a.is_terminal]
        self.completed = len(done)
        self.passed = sum(1 for a in done if a.reward == 1)
        self.failed = sum(1 for a in done if a.reward == 0)
        self.errored = sum(1 for a in done if a.reward is None)
        self.valid_attempts = self.passed + self.failed
        self.pass_at_k = self.passed > 0
        self.pass_rate = (
            self.passed / self.valid_attempts if self.valid_attempts else None
        )
        self.stats = RunStats.over(self.attempts)


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
    running_attempts: int = 0
    queued_attempts: int = 0
    valid_attempts: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0

    # Fraction of TASKS with at least one passing attempt -- the headline number.
    pass_at_k_rate: float | None = None
    # Mean reward over valid attempts only.
    mean_reward: float | None = None

    stats: RunStats = Field(default_factory=RunStats)
    total_tokens: int = 0
    error: str | None = None

    def recompute(self) -> None:
        for task in self.tasks:
            task.recompute(self.spec.attempts_per_task)

        every_attempt = [a for t in self.tasks for a in t.attempts]
        self.total_attempts = self.spec.total_attempts
        self.completed_attempts = sum(t.completed for t in self.tasks)
        self.running_attempts = sum(t.running for t in self.tasks)
        self.queued_attempts = sum(t.queued for t in self.tasks)
        self.passed = sum(t.passed for t in self.tasks)
        self.failed = sum(t.failed for t in self.tasks)
        self.errored = sum(t.errored for t in self.tasks)
        self.valid_attempts = self.passed + self.failed

        self.stats = RunStats.over(every_attempt)
        self.total_tokens = self.stats.total_tokens

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
