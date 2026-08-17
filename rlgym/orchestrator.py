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

**Attempts are ordered task-major**: a task's K attempts all run before the next task
starts. A job that is halfway done then has *complete* results for half the tasks --
pass@k for those tasks is final and usable -- rather than one sample of every task, where
no task's pass@k is settled and nothing can be relied on until the whole job finishes.
Finished work you can use beats uniform progress you can't.

**Job state covers attempts that have not run yet.** `job.json` carries a row for every one
of the T x K attempts from the moment the job starts, so the UI can show the full grid with
queued attempts greyed out. Rows are refreshed from the attempt artifacts on disk by a
watcher thread, which is what makes provisioning and the agent loop visible while they
happen -- the executor and its worker both write phases into artifact.json, so polling those
files needs no callback plumbing through the subprocess boundary.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from . import executor, gym_stack, ports, tasks as tasks_module, vendor
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

# How often the watcher re-reads in-flight artifacts. Provisioning takes ~35s and a step
# takes seconds, so this is fast enough to look live and slow enough to be free.
WATCH_INTERVAL_S = 2.0


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
    """Load a job document, with its derived numbers recomputed from its attempts.

    Recomputing on read rather than trusting the stored totals means a job.json written
    before a counter existed still reports that counter correctly -- the attempt rows are
    the source of truth, and everything else is a view of them.
    """
    path = job_dir(job_id) / JOB_FILE
    if not path.is_file():
        return None
    try:
        job = JobResult.model_validate_json(path.read_text())
    except Exception:
        return None
    job.recompute()
    return job


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
        error=artifact.error,
        diff=evaluation.diff if evaluation else None,
        has_video=bool(artifact.video_path),
        screenshot_count=len(list(steps_dir.glob("*.png"))) if steps_dir.is_dir() else 0,
    )


def _read_evaluation(attempt_path) -> EvaluationResult | None:
    eval_path = attempt_path / executor.EVALUATION_NAME
    if not eval_path.is_file():
        return None
    try:
        return EvaluationResult.model_validate_json(eval_path.read_text())
    except Exception:
        return None


def _supersedes(candidate: AttemptArtifact, incumbent: AttemptArtifact) -> bool:
    """Which of two artifacts filling the same (task, index) slot is the real one?

    Two can exist after an interrupted run: a retry gets a fresh attempt_id and therefore a
    fresh directory, leaving the abandoned attempt's artifact on disk beside it. A finished
    attempt always wins over an unfinished one -- otherwise a stale IN_PROGRESS artifact
    would show a completed slot as forever running -- and between two of the same kind, the
    one that started later is the live one.
    """
    if candidate.is_terminal != incumbent.is_terminal:
        return candidate.is_terminal
    if candidate.started_at and incumbent.started_at:
        return candidate.started_at > incumbent.started_at
    return bool(candidate.started_at)


def load_attempts(
    job_id: str, *, skip: set[tuple[str, int]] | None = None
) -> dict[tuple[str, int], AttemptSummary]:
    """Every attempt on disk for this job, keyed by (task_id, attempt_index).

    `skip` omits slots the caller already holds a terminal result for. Artifacts grow to
    megabytes over a 40-step episode, so re-reading finished ones on every poll would make
    the watcher's cost grow with the job instead of with the handful of attempts in flight.
    """
    found: dict[tuple[str, int], AttemptArtifact] = {}
    root = job_dir(job_id)
    if not root.is_dir():
        return {}

    for attempt_path in sorted(root.iterdir()):
        if not attempt_path.is_dir():
            continue
        artifact = executor.read_artifact(attempt_path)
        if artifact is None:
            continue
        slot = (artifact.task_id, artifact.attempt_index)
        if skip and slot in skip:
            continue
        incumbent = found.get(slot)
        if incumbent is None or _supersedes(artifact, incumbent):
            found[slot] = artifact

    return {
        slot: summarize(
            artifact, _read_evaluation(executor.attempt_dir(artifact.attempt_id, job_id))
        )
        for slot, artifact in found.items()
    }


def load_completed_attempts(job_id: str) -> dict[tuple[str, int], AttemptSummary]:
    """The subset of `load_attempts` that already reached a terminal state.

    This is what a resumed job skips, so it is exactly the work that is already done.
    NOT_OBSERVED attempts count as done rather than being retried -- attempt-level retry is
    deliberately off (transient 429s are absorbed by the SDK's backoff below the attempt
    boundary, and K attempts provide the redundancy).
    """
    return {
        slot: summary
        for slot, summary in load_attempts(job_id).items()
        if summary.is_terminal
    }


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
    # Every requested attempt gets a row up front, so the UI can render the whole T x K
    # grid immediately. Rows for attempts that have not started carry no attempt_id, which
    # is what marks them unopenable.
    result.tasks = [
        TaskResult(
            task_id=task.id,
            prompt=task.task,
            expected_output=task.expected_output(),
            attempts=[
                AttemptSummary(task_id=task.id, attempt_index=index)
                for index in range(spec.attempts_per_task)
            ],
        )
        for task in selected
    ]

    lock = threading.Lock()
    # Slots we already hold a finished result for. Never re-read from disk.
    settled: set[tuple[str, int]] = set()
    last_signature: list[tuple] = []

    def _publish(force: bool) -> bool:
        """Recompute and persist, skipping the write when nothing actually moved.

        Caller must hold `lock`. Returns whether anything changed, so the caller can decide
        about `on_update` outside the lock.
        """
        result.recompute()
        signature = [
            (a.attempt_id, a.status, a.phase, a.step_count)
            for t in result.tasks
            for a in t.attempts
        ]
        if not force and signature == last_signature:
            return False
        last_signature[:] = signature
        write_job(result)
        return True

    def _slot(task_id: str, index: int) -> tuple[TaskResult, int] | None:
        for task_result in result.tasks:
            if task_result.task_id != task_id:
                continue
            for i, row in enumerate(task_result.attempts):
                if row.attempt_index == index:
                    return task_result, i
        return None

    def apply(summary: AttemptSummary) -> None:
        """Record a finished attempt from the value the executor returned.

        Deliberately not re-read from disk: the artifact in hand IS the result, and making
        the job's numbers depend on a file having been written would mean a successful
        attempt could vanish from the tally over an I/O error.
        """
        with lock:
            found = _slot(summary.task_id, summary.attempt_index)
            if found is not None:
                task_result, i = found
                task_result.attempts[i] = summary
            if summary.is_terminal:
                settled.add((summary.task_id, summary.attempt_index))
            changed = _publish(force=True)
        if changed and on_update:
            on_update(result)

    def refresh(force: bool = False) -> None:
        """Pull *in-flight* attempt state off disk into the job document.

        Both the parent (provisioning, auth) and the worker (connecting, running, each
        step) rewrite artifact.json as they go, so reading those files is the whole of the
        progress mechanism -- no callbacks have to cross the subprocess boundary. Attempts
        already settled by `apply` are skipped, so this only ever describes work in
        progress and can never overwrite a finished result.
        """
        live = load_attempts(spec.job_id, skip=settled)
        with lock:
            for slot, summary in live.items():
                found = _slot(*slot)
                if found is None:
                    continue
                task_result, i = found
                task_result.attempts[i] = summary
                if summary.is_terminal:
                    settled.add(slot)
            changed = _publish(force)
        if changed and on_update:
            on_update(result)

    already = load_completed_attempts(spec.job_id) if resume else {}
    refresh(force=True)

    # task-major: a task's K attempts finish together, so its pass@k settles as early as
    # possible instead of every task staying provisional until the whole job ends.
    queue: list[tuple[Task, int]] = [
        (task, index)
        for task in selected
        for index in range(spec.attempts_per_task)
        if (task.id, index) not in already
    ]

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
        apply(summarize(artifact, evaluation))

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
        # Polls the in-flight attempts' artifacts so provisioning and the agent loop are
        # visible while they happen. Daemon, so it can never hold the process open.
        stop_watching = threading.Event()

        def watch() -> None:
            while not stop_watching.wait(WATCH_INTERVAL_S):
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    # Progress reporting must never be able to fail a job. A transient
                    # read of a half-written artifact is not worth losing the run over.
                    pass

        watcher = threading.Thread(
            target=watch, name=f"watch-{spec.job_id}", daemon=True
        )
        watcher.start()
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
        finally:
            stop_watching.set()
            watcher.join(timeout=WATCH_INTERVAL_S * 2)

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
