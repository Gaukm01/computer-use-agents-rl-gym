"""Run a job: T tasks x K attempts, in parallel, resumable.

In RL terms this is the rollout-collection layer. It is named `orchestrator` rather than
`rollout` because we already pinned "rollout" to mean a single attempt -- a file named
rollout.py that handled batches would overload the one term we defined precisely.

It adds orchestration and nothing else: every attempt goes through the same
`executor.execute_attempt()` that the single-task script calls, so there is no second code
path to keep in sync, and no behaviour that only appears in batch mode.

**Threads here, subprocesses underneath.** `execute_attempt()` already forks a subprocess
per attempt and blocks waiting on it, so the pool only needs to supervise. Threads are
therefore correct despite the GIL -- they are parked in `Popen.wait()`, not computing --
and Playwright never runs in this process, which is what would have made threads unusable.

**Attempts are ordered attempt-major**: every task's attempt 0 runs before any task's
attempt 1. A job that is halfway done then has one sample of every task rather than all
samples of half the tasks, which is far more useful to look at while it runs.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from . import executor, gym_stack, ports, schemas, tasks as tasks_module, vendor
from .schemas import (
    AgentConfig,
    AttemptArtifact,
    AttemptSummary,
    EvaluationResult,
    JobResult,
    JobSpec,
    JobStatus,
    Limits,
    Task,
    TaskResult,
)

JOB_FILE = "job.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    # Sortable-ish prefix so `ls runs/` reads chronologically without opening anything.
    return f"job-{_now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def job_dir(job_id: str):
    return vendor.RUNS_DIR / job_id


def write_job(result: JobResult) -> None:
    """Persist job state. Same temp-then-replace trick as artifacts, so a poller never
    reads a half-written document."""
    out = job_dir(result.spec.job_id)
    out.mkdir(parents=True, exist_ok=True)
    path = out / JOB_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(result.model_dump_json(indent=2))
    tmp.replace(path)


def read_job(job_id: str) -> JobResult | None:
    path = job_dir(job_id) / JOB_FILE
    if not path.is_file():
        return None
    try:
        return JobResult.model_validate_json(path.read_text())
    except Exception:
        return None


def summarize(
    artifact: AttemptArtifact, evaluation: EvaluationResult | None
) -> AttemptSummary:
    out_dir = executor.attempt_dir(artifact.attempt_id, artifact.job_id)
    steps_dir = out_dir / executor.STEPS_DIR
    return AttemptSummary(
        attempt_id=artifact.attempt_id,
        task_id=artifact.task_id,
        attempt_index=artifact.attempt_index,
        status=artifact.status,
        phase=artifact.phase,
        reward=artifact.reward,
        verdict=evaluation.verdict if evaluation else None,
        near_miss=evaluation.near_miss if evaluation else False,
        step_count=artifact.step_count,
        duration_s=artifact.duration_s,
        total_tokens=artifact.token_usage.total_tokens,
        used_sql_editor=artifact.used_sql_editor,
        error=artifact.error,
        diff=evaluation.diff if evaluation else None,
        has_video=bool(artifact.video_path),
        screenshot_count=len(list(steps_dir.glob("*.png"))) if steps_dir.is_dir() else 0,
    )


def load_completed_attempts(job_id: str) -> dict[tuple[str, int], AttemptSummary]:
    """Find attempts from a previous run of this job that already reached a terminal state.

    Keyed by (task_id, attempt_index) so a resumed job skips exactly the work that is
    already done. ERRORED attempts are treated as done rather than retried -- attempt-level
    retry is deliberately off (transient 429s are absorbed by the SDK's backoff below the
    attempt boundary, and K attempts provide the redundancy).
    """
    found: dict[tuple[str, int], AttemptSummary] = {}
    root = job_dir(job_id)
    if not root.is_dir():
        return found

    for attempt_path in sorted(root.iterdir()):
        if not attempt_path.is_dir():
            continue
        artifact = executor.read_artifact(attempt_path)
        if artifact is None or artifact.status not in schemas.TERMINAL_STATUSES:
            continue
        evaluation = None
        eval_path = attempt_path / executor.EVALUATION_NAME
        if eval_path.is_file():
            try:
                evaluation = EvaluationResult.model_validate_json(eval_path.read_text())
            except Exception:
                evaluation = None
        found[(artifact.task_id, artifact.attempt_index)] = summarize(
            artifact, evaluation
        )
    return found


def build_spec(
    task_ids: list[str],
    *,
    attempts_per_task: int = 1,
    concurrency: int = 3,
    agent_config: AgentConfig | None = None,
    limits: Limits | None = None,
    headless: bool = True,
    record_video: bool = True,
    tasks_file: str | None = None,
    job_id: str | None = None,
) -> JobSpec:
    return JobSpec(
        job_id=job_id or new_job_id(),
        task_ids=task_ids,
        tasks_file=tasks_file,
        attempts_per_task=attempts_per_task,
        concurrency=concurrency,
        agent_config=agent_config or AgentConfig(),
        limits=limits or Limits(),
        headless=headless,
        record_video=record_video,
        created_at=_now(),
    )


def run_job(
    spec: JobSpec,
    *,
    resume: bool = True,
    cancel: threading.Event | None = None,
    on_update=None,
) -> JobResult:
    """Execute a job to completion and return its final state.

    `cancel` stops *scheduling* new attempts; already-running ones finish so their videos
    and artifacts are written rather than truncated.

    `on_update(JobResult)` fires after every attempt so a caller can stream progress.
    """
    cancel = cancel or threading.Event()
    task_map = tasks_module.load_task_map(spec.tasks_file)
    selected: list[Task] = [task_map[t] for t in spec.task_ids if t in task_map]

    result = JobResult(spec=spec, status=JobStatus.RUNNING, started_at=_now())
    result.tasks = [
        TaskResult(
            task_id=task.id, prompt=task.task, expected_output=task.expected_output()
        )
        for task in selected
    ]
    by_task = {t.task_id: t for t in result.tasks}

    already = load_completed_attempts(spec.job_id) if resume else {}
    for (task_id, _index), summary in sorted(already.items()):
        if task_id in by_task:
            by_task[task_id].attempts.append(summary)

    result.recompute()
    write_job(result)
    if on_update:
        on_update(result)

    # attempt-major: one sample of everything before a second sample of anything
    queue: list[tuple[Task, int]] = [
        (task, index)
        for index in range(spec.attempts_per_task)
        for task in selected
        if (task.id, index) not in already
    ]

    lock = threading.Lock()

    def run_attempt(task: Task, index: int) -> None:
        artifact = executor.execute_attempt(
            task,
            agent_config=spec.agent_config,
            limits=spec.limits,
            job_id=spec.job_id,
            attempt_index=index,
            headless=spec.headless,
            record_video=spec.record_video,
        )
        evaluation = executor.grade_attempt(artifact, task)
        summary = summarize(artifact, evaluation)
        with lock:
            by_task[task.id].attempts.append(summary)
            by_task[task.id].attempts.sort(key=lambda a: a.attempt_index)
            result.recompute()
            write_job(result)
        if on_update:
            on_update(result)

    if queue:
        # Remove containers orphaned by a previous Ctrl-C or a killed API server, else the
        # next run fails with "port already allocated". NOTE: this removes every ephemeral
        # container, so starting a second job while one runs would destroy the first job's
        # gyms -- jobs are expected to run one at a time.
        found, removed, sweep_error = gym_stack.sweep()
        if sweep_error and removed < found:
            # Not fatal -- provisioning will fail loudly per attempt if ports are still
            # held -- but silence here previously hid a degraded Docker daemon.
            log_line = f"sweep: {removed}/{found} orphans removed; {sweep_error}"
            print(log_line)
        try:
            # Capped at the port allocator: an extra thread would only block in claim().
            workers = max(1, min(spec.concurrency, ports.MAX_GYMS))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures: list[Future] = []
                for task, index in queue:
                    if cancel.is_set():
                        break
                    futures.append(pool.submit(run_attempt, task, index))
                for future in futures:
                    # Surface a crash in the supervising thread rather than losing it.
                    future.result()
        except Exception as exc:  # noqa: BLE001
            result.status = JobStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"

    if result.status is not JobStatus.FAILED:
        result.status = JobStatus.CANCELLED if cancel.is_set() else JobStatus.COMPLETED

    result.ended_at = _now()
    if result.started_at:
        result.duration_s = (result.ended_at - result.started_at).total_seconds()
    result.recompute()
    write_job(result)
    if on_update:
        on_update(result)
    return result
