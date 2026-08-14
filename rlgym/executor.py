"""Run exactly one attempt and produce its artifact.

This is the atomic unit of the whole system: the job runner calls this T x K times, and the
API calls the runner. Nothing above it re-implements any of it.

**Each attempt runs in its own subprocess**, which is a requirement rather than a
preference:

  * Playwright's *sync* API is not thread-safe -- several `sync_playwright()` instances
    across threads fail outright, so threads can't be used for parallelism.
  * A wall-clock budget has to be enforceable against a genuinely hung process. You can
    SIGKILL a subprocess; you cannot reliably interrupt a blocked sync call in a thread.
  * A crashing browser or a segfaulting driver takes down one attempt, not the whole job.

Status assignment lives here, with one deliberate hand-off: an attempt that submitted an
answer is written as COMPLETED_INCORRECT ("completed, pending grading") and only the grader
promotes it to COMPLETED_CORRECT. That keeps the reward function in exactly one place.

The artifact is rewritten after every step, so a killed attempt still yields the trajectory
up to the kill, and so a UI can watch progress live.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

from .agents.base import AgentRunResult
from .grader import grade
from .schemas import (
    AgentConfig,
    AttemptArtifact,
    AttemptStatus,
    EvaluationResult,
    Limits,
    StepRecord,
    StopReason,
    Task,
)
from .vendor import PROJECT_ROOT, RUNS_DIR, STORAGE_STATE

# Extra wall-clock the parent allows beyond the agent's own soft deadline, so a clean
# shutdown (closing the context, flushing the video) can finish before the SIGKILL lands.
SHUTDOWN_GRACE_S = 45

ARTIFACT_NAME = "artifact.json"
EVALUATION_NAME = "evaluation.json"
SPEC_NAME = "spec.json"
WORKER_LOG = "worker.log"
STEPS_DIR = "steps"
VIDEO_DIR = "video"

# How an agent's reason for stopping becomes an attempt status.
STOP_REASON_TO_STATUS: dict[StopReason, AttemptStatus] = {
    # "Completed, pending grading" -- the grader promotes a correct answer.
    StopReason.SUBMITTED: AttemptStatus.COMPLETED_INCORRECT,
    # Had budget, produced no answer. Genuine agent failure.
    StopReason.NO_FUNCTION_CALL: AttemptStatus.COMPLETED_INCORRECT,
    StopReason.MAX_STEPS: AttemptStatus.MAX_STEPS_EXCEEDED,
    StopReason.TIMEOUT: AttemptStatus.TIMED_OUT,
    # Outcome never observed -> reward None, excluded from pass@k.
    StopReason.API_ERROR: AttemptStatus.ERRORED,
    StopReason.HARNESS_ERROR: AttemptStatus.ERRORED,
    # A provider-side refusal also means we failed to observe the agent's capability.
    StopReason.SAFETY_BLOCKED: AttemptStatus.ERRORED,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_attempt_id(task_id: str, attempt_index: int) -> str:
    return f"{task_id}-a{attempt_index}-{uuid.uuid4().hex[:8]}"


def attempt_dir(attempt_id: str, job_id: str | None = None) -> pathlib.Path:
    return RUNS_DIR / (job_id or "adhoc") / attempt_id


def write_artifact(artifact: AttemptArtifact, out_dir: pathlib.Path) -> None:
    """Atomic-ish write: temp file then replace, so a reader never sees half a document."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ARTIFACT_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(artifact.model_dump_json(indent=2))
    tmp.replace(path)


def read_artifact(out_dir: pathlib.Path) -> AttemptArtifact | None:
    path = out_dir / ARTIFACT_NAME
    if not path.is_file():
        return None
    try:
        return AttemptArtifact.model_validate_json(path.read_text())
    except Exception:
        return None


def build_prompt(task: Task, agent_config: AgentConfig) -> str:
    """The task text, plus an optional hint. The submit-answer contract is appended by
    the agent itself, since it describes that agent's tooling."""
    prompt = task.task
    if agent_config.hint:
        prompt = f"{prompt}\n\nHint: {agent_config.hint}"
    return prompt


# ======================================================================================
# Worker: runs inside the subprocess
# ======================================================================================


def run_attempt_worker(spec_path: pathlib.Path) -> int:
    """Execute one attempt in this process. Writes the artifact as it goes."""
    load_dotenv(PROJECT_ROOT / ".env")

    spec = json.loads(spec_path.read_text())
    task = Task.model_validate(spec["task"])
    agent_config = AgentConfig.model_validate(spec["agent_config"])
    limits = Limits.model_validate(spec["limits"])
    out_dir = pathlib.Path(spec["out_dir"])
    gym_url = spec["gym_url"]
    headless = bool(spec.get("headless", True))
    record_video = bool(spec.get("record_video", True))

    artifact = AttemptArtifact(
        attempt_id=spec["attempt_id"],
        task_id=task.id,
        job_id=spec.get("job_id"),
        attempt_index=int(spec.get("attempt_index", 0)),
        agent_config=agent_config,
        limits=limits,
        gym_url=gym_url,
        status=AttemptStatus.IN_PROGRESS,
        started_at=_now(),
    )
    write_artifact(artifact, out_dir)

    def finalize(result: AgentRunResult | None = None) -> None:
        artifact.ended_at = _now()
        if artifact.started_at:
            artifact.duration_s = (
                artifact.ended_at - artifact.started_at
            ).total_seconds()
        if result is not None:
            artifact.steps = result.steps
            artifact.step_count = len(result.steps)
            artifact.token_usage = result.token_usage
            artifact.safety_acknowledgements = result.safety_acknowledgements
        write_artifact(artifact, out_dir)

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or api_key == "PASTE_YOUR_KEY_HERE":
        artifact.status = AttemptStatus.ERRORED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        artifact.error = "GEMINI_API_KEY missing from .env"
        finalize()
        return 1

    # Imported here so an import failure is captured as ERRORED rather than crashing
    # before the artifact exists.
    try:
        from .agents.gemini_cu import GeminiComputerUseAgent
        from .computer import GymComputer
    except Exception as exc:  # noqa: BLE001
        artifact.status = AttemptStatus.ERRORED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        artifact.error = f"import failed: {type(exc).__name__}: {exc}"
        finalize()
        return 1

    steps_dir = out_dir / STEPS_DIR
    video_dir = (out_dir / VIDEO_DIR) if record_video else None
    storage_state = STORAGE_STATE if STORAGE_STATE.is_file() else None

    result: AgentRunResult | None = None
    # The agent stops itself slightly before the parent's kill deadline.
    deadline = time.monotonic() + limits.timeout_s

    try:
        with GymComputer(
            gym_url=gym_url,
            storage_state=storage_state,
            video_dir=video_dir,
            headless=headless,
            prohibited_actions=tuple(agent_config.prohibited_actions),
        ) as computer:
            computer.wait_for_ready()

            # Pre-flight: a stale cookie is an INFRASTRUCTURE failure. Without this check
            # the agent would spend its whole budget staring at a login form and the run
            # would look like an analysis failure.
            if storage_state is None:
                artifact.status = AttemptStatus.ERRORED
                artifact.stop_reason = StopReason.HARNESS_ERROR
                artifact.error = (
                    f"no storage_state at {STORAGE_STATE}; run "
                    "`python -m rlgym.capture_auth` first"
                )
                finalize()
                return 1
            if not computer.is_authenticated():
                artifact.status = AttemptStatus.ERRORED
                artifact.stop_reason = StopReason.HARNESS_ERROR
                artifact.error = (
                    "not authenticated after injecting storage_state (session likely "
                    "expired) -- re-run `python -m rlgym.capture_auth`"
                )
                finalize()
                return 1

            agent = GeminiComputerUseAgent(agent_config, api_key=api_key)

            def on_step(step: StepRecord) -> None:
                # Persist after every action so a killed attempt keeps its trajectory,
                # and so a UI can follow along live.
                artifact.steps.append(step)
                artifact.step_count = len(artifact.steps)
                artifact.used_sql_editor = computer.used_sql_editor
                artifact.refusals = list(computer.refusals)
                write_artifact(artifact, out_dir)

            result = agent.run(
                prompt=build_prompt(task, agent_config),
                computer=computer,
                max_steps=limits.max_steps,
                screenshot_dir=steps_dir,
                on_step=on_step,
                deadline=deadline,
            )

            artifact.used_sql_editor = computer.used_sql_editor
            artifact.refusals = list(computer.refusals)

        # Outside the context: closing it flushed the video, so the path now exists.
        if getattr(computer, "video_path", None):
            try:
                artifact.video_path = str(
                    computer.video_path.relative_to(out_dir)  # type: ignore[union-attr]
                )
            except ValueError:
                artifact.video_path = str(computer.video_path)

    except Exception as exc:  # noqa: BLE001 -- harness failure, not agent failure
        artifact.status = AttemptStatus.ERRORED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        artifact.error = f"{type(exc).__name__}: {exc}"
        finalize(result)
        return 1

    artifact.stop_reason = result.stop_reason
    artifact.status = STOP_REASON_TO_STATUS.get(
        result.stop_reason, AttemptStatus.ERRORED
    )
    artifact.actual_output = result.submitted_answer
    artifact.actual_output_raw = result.submitted_answer_raw
    artifact.final_text = result.final_text
    artifact.error = result.error
    finalize(result)
    return 0


# ======================================================================================
# Parent: spawns the worker, enforces the wall clock
# ======================================================================================


def execute_attempt(
    task: Task,
    *,
    agent_config: AgentConfig | None = None,
    limits: Limits | None = None,
    gym_url: str | None = None,
    job_id: str | None = None,
    attempt_index: int = 0,
    headless: bool = True,
    record_video: bool = True,
) -> AttemptArtifact:
    """Run one attempt in a subprocess and return its artifact."""
    agent_config = agent_config or AgentConfig()
    limits = limits or Limits(
        max_steps=task.max_steps or Limits().max_steps,
        timeout_s=task.timeout_s or Limits().timeout_s,
    )
    gym_url = (gym_url or os.environ.get("METABASE_URL") or "http://localhost:3000").rstrip("/")

    attempt_id = new_attempt_id(task.id, attempt_index)
    out_dir = attempt_dir(attempt_id, job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = {
        "attempt_id": attempt_id,
        "job_id": job_id,
        "attempt_index": attempt_index,
        "task": task.model_dump(),
        "agent_config": agent_config.model_dump(),
        "limits": limits.model_dump(),
        "out_dir": str(out_dir),
        "gym_url": gym_url,
        "headless": headless,
        "record_video": record_video,
    }
    spec_path = out_dir / SPEC_NAME
    spec_path.write_text(json.dumps(spec, indent=2))

    cmd = [sys.executable, "-m", "rlgym.executor", "--worker", str(spec_path)]
    log_path = out_dir / WORKER_LOG

    timed_out = False
    with log_path.open("w") as log:
        # start_new_session gives the worker its own process group, so a timeout can kill
        # the browser and driver it spawned rather than orphaning them.
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=limits.timeout_s + SHUTDOWN_GRACE_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass

    artifact = read_artifact(out_dir)
    if artifact is None:
        # The worker died before writing anything -- an import error or an OOM kill.
        tail = ""
        if log_path.is_file():
            tail = log_path.read_text()[-1500:]
        return AttemptArtifact(
            attempt_id=attempt_id,
            task_id=task.id,
            job_id=job_id,
            attempt_index=attempt_index,
            agent_config=agent_config,
            limits=limits,
            gym_url=gym_url,
            status=AttemptStatus.ERRORED,
            stop_reason=StopReason.HARNESS_ERROR,
            error=f"worker produced no artifact (exit {proc.returncode}). Log tail:\n{tail}",
            started_at=_now(),
            ended_at=_now(),
        )

    if timed_out:
        # Hard kill: the worker never got to classify itself. Its partial trajectory
        # survives because the artifact is rewritten every step.
        artifact.status = AttemptStatus.TIMED_OUT
        artifact.stop_reason = StopReason.TIMEOUT
        artifact.error = (
            artifact.error
            or f"killed after {limits.timeout_s + SHUTDOWN_GRACE_S}s wall clock"
        )
        write_artifact(artifact, out_dir)
    elif artifact.status is AttemptStatus.IN_PROGRESS:
        # Exited without finalizing: a crash the worker's own handler didn't catch.
        artifact.status = AttemptStatus.ERRORED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        artifact.error = artifact.error or (
            f"worker exited with code {proc.returncode} mid-attempt"
        )
        write_artifact(artifact, out_dir)

    return artifact


def grade_attempt(artifact: AttemptArtifact, task: Task) -> EvaluationResult:
    """Grade an attempt, promote a correct one, and persist both records.

    Kept next to the executor so `run_one` and the job runner share identical semantics:
    the executor never decides correctness, and the grader is the only thing that can set
    COMPLETED_CORRECT.
    """
    evaluation = grade(artifact, task)
    out_dir = attempt_dir(artifact.attempt_id, artifact.job_id)

    if evaluation.verdict == "pass":
        artifact.status = AttemptStatus.COMPLETED_CORRECT
        write_artifact(artifact, out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EVALUATION_NAME).write_text(evaluation.model_dump_json(indent=2))
    return evaluation


def main() -> int:
    parser = argparse.ArgumentParser(description="rlgym attempt executor")
    parser.add_argument(
        "--worker",
        type=pathlib.Path,
        required=True,
        help="Path to a spec.json. Internal -- use rlgym.run_one instead.",
    )
    args = parser.parse_args()
    return run_attempt_worker(args.worker)


if __name__ == "__main__":
    sys.exit(main())
