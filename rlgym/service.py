"""Service layer: everything the platform can do, with no web framework in sight.

This is the seam the API will sit on. Nothing here imports FastAPI, so the HTTP layer
becomes pure wiring -- route -> service call -> pydantic model -> JSON -- and the same
functions stay usable from a CLI, a notebook, or a test without spinning up a server.

Jobs run in a background thread and stream their state to `runs/<job_id>/job.json`, so
progress is readable by polling a file. That means a UI can follow a running job without
any websocket plumbing, and a job survives being observed by nobody.

Known limitation, deliberate for now: the registry of *running* jobs lives in memory, so
restarting the API abandons in-flight jobs. Their artifacts and job.json survive, and
`resume_job()` picks them up where they stopped. Moving the registry into a DB or a queue
is the Milestone 3 answer, not something to fake here.
"""

from __future__ import annotations

import pathlib
import threading
from typing import Any

from . import executor, orchestrator, tasks as tasks_module, vendor
from .schemas import (
    AgentConfig,
    AttemptArtifact,
    EvaluationResult,
    JobResult,
    JobSpec,
    JobStatus,
    Limits,
    Task,
)


class _RunningJob:
    __slots__ = ("thread", "cancel")

    def __init__(self, thread: threading.Thread, cancel: threading.Event) -> None:
        self.thread = thread
        self.cancel = cancel


_running: dict[str, _RunningJob] = {}
_registry_lock = threading.Lock()


# ======================================================================================
# Tasks
# ======================================================================================


def list_tasks(tasks_file: str | pathlib.Path | None = None) -> list[Task]:
    """Every task the platform can run. The UI calls this to render the task picker."""
    return tasks_module.load_tasks(tasks_file)


def get_task(task_id: str, tasks_file: str | pathlib.Path | None = None) -> Task | None:
    return tasks_module.load_task_map(tasks_file).get(task_id)


# ======================================================================================
# Jobs
# ======================================================================================


def submit_job(
    task_ids: list[str] | None = None,
    *,
    attempts_per_task: int = 1,
    concurrency: int = 3,
    model: str | None = None,
    hint: str | None = None,
    max_steps: int | None = None,
    timeout_s: int | None = None,
    gym_url: str = "http://localhost:3000",
    headless: bool = True,
    record_video: bool = True,
    tasks_file: str | pathlib.Path | None = None,
    job_id: str | None = None,
    block: bool = False,
) -> JobResult:
    """Start a job and return immediately with its QUEUED/RUNNING state.

    `task_ids=None` means every task in the file. `block=True` runs inline instead of in a
    thread, which is what tests and the CLI want.
    """
    available = tasks_module.load_task_map(tasks_file)
    selected = list(available) if not task_ids else task_ids

    unknown = [t for t in selected if t not in available]
    if unknown:
        raise KeyError(
            f"unknown task id(s): {', '.join(unknown)}. Available: {', '.join(available)}"
        )
    if attempts_per_task < 1:
        raise ValueError("attempts_per_task must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    defaults = Limits()
    spec = orchestrator.build_spec(
        selected,
        attempts_per_task=attempts_per_task,
        concurrency=concurrency,
        agent_config=AgentConfig(
            model=model or AgentConfig().model,
            hint=hint,
        ),
        limits=Limits(
            max_steps=max_steps or defaults.max_steps,
            timeout_s=timeout_s or defaults.timeout_s,
        ),
        gym_url=gym_url,
        headless=headless,
        record_video=record_video,
        tasks_file=str(tasks_file) if tasks_file else None,
        job_id=job_id,
    )

    if block:
        return orchestrator.run_job(spec, resume=True)

    return _start_background(spec, resume=True)


def resume_job(job_id: str, *, block: bool = False) -> JobResult:
    """Continue a job that was interrupted. Attempts already in a terminal state are
    skipped, so this costs nothing for work already done."""
    existing = orchestrator.read_job(job_id)
    if existing is None:
        raise KeyError(f"unknown job {job_id!r}")
    if is_running(job_id):
        return existing
    if block:
        return orchestrator.run_job(existing.spec, resume=True)
    return _start_background(existing.spec, resume=True)


def _start_background(spec: JobSpec, *, resume: bool) -> JobResult:
    cancel = threading.Event()

    def target() -> None:
        try:
            orchestrator.run_job(spec, resume=resume, cancel=cancel)
        finally:
            with _registry_lock:
                _running.pop(spec.job_id, None)

    thread = threading.Thread(
        target=target, name=f"job-{spec.job_id}", daemon=True
    )
    with _registry_lock:
        _running[spec.job_id] = _RunningJob(thread, cancel)
    thread.start()

    # Give run_job a moment to write the initial job.json so the caller gets real state
    # rather than a placeholder it has to poll for.
    thread.join(timeout=1.0)
    return orchestrator.read_job(spec.job_id) or JobResult(
        spec=spec, status=JobStatus.QUEUED
    )


def get_job(job_id: str) -> JobResult | None:
    return orchestrator.read_job(job_id)


def list_jobs(limit: int | None = None) -> list[JobResult]:
    """Newest first. Reads job.json files -- no index to keep in sync."""
    if not vendor.RUNS_DIR.is_dir():
        return []
    jobs: list[JobResult] = []
    for path in vendor.RUNS_DIR.iterdir():
        if not path.is_dir() or not (path / orchestrator.JOB_FILE).is_file():
            continue
        job = orchestrator.read_job(path.name)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.spec.created_at or j.started_at, reverse=True)
    return jobs[:limit] if limit else jobs


def is_running(job_id: str) -> bool:
    with _registry_lock:
        entry = _running.get(job_id)
    return bool(entry and entry.thread.is_alive())


def cancel_job(job_id: str) -> bool:
    """Stop scheduling new attempts. Running attempts finish so their artifacts and
    videos are written rather than truncated."""
    with _registry_lock:
        entry = _running.get(job_id)
    if entry is None:
        return False
    entry.cancel.set()
    return True


# ======================================================================================
# Attempts
# ======================================================================================


def get_attempt(job_id: str, attempt_id: str) -> dict[str, Any] | None:
    """Full trajectory for one attempt: artifact + verdict + media paths.

    This is what the transcript viewer renders -- step-by-step actions, the model's
    reasoning, screenshots and the video, alongside expected vs actual.
    """
    out_dir = executor.attempt_dir(attempt_id, job_id)
    artifact = executor.read_artifact(out_dir)
    if artifact is None:
        return None

    evaluation: EvaluationResult | None = None
    eval_path = out_dir / executor.EVALUATION_NAME
    if eval_path.is_file():
        try:
            evaluation = EvaluationResult.model_validate_json(eval_path.read_text())
        except Exception:
            evaluation = None

    return {
        "artifact": artifact,
        "evaluation": evaluation,
        "screenshots": attempt_screenshots(job_id, attempt_id),
        "video": attempt_video(job_id, attempt_id),
    }


def attempt_screenshots(job_id: str, attempt_id: str) -> list[str]:
    """Step screenshots in step order, as paths relative to the attempt directory."""
    steps_dir = executor.attempt_dir(attempt_id, job_id) / executor.STEPS_DIR
    if not steps_dir.is_dir():
        return []
    return [f"{executor.STEPS_DIR}/{p.name}" for p in sorted(steps_dir.glob("*.png"))]


def attempt_video(job_id: str, attempt_id: str) -> str | None:
    video_dir = executor.attempt_dir(attempt_id, job_id) / executor.VIDEO_DIR
    if not video_dir.is_dir():
        return None
    videos = sorted(video_dir.glob("*.webm"))
    return f"{executor.VIDEO_DIR}/{videos[0].name}" if videos else None


def attempt_file(job_id: str, attempt_id: str, relative: str) -> pathlib.Path | None:
    """Resolve a media path for the HTTP layer to serve.

    Path traversal is checked here rather than at the route, so every caller gets the
    protection -- a relative path is only returned if it genuinely resolves inside the
    attempt's own directory.
    """
    base = executor.attempt_dir(attempt_id, job_id).resolve()
    target = (base / relative).resolve()
    if not target.is_file() or base not in target.parents:
        return None
    return target
