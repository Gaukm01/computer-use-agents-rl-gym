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

**Each attempt also gets its own gym.** `execute_attempt()` provisions an ephemeral Metabase
+ Postgres stack, runs the worker against it, and destroys it. The provisioning wraps the
`Popen` in THIS process, never inside the worker: a timeout kills the worker with SIGKILL,
which runs no `finally`, so teardown placed there would leak a container and a port on
precisely the path that most needs cleaning up.

Status assignment lives here, with one deliberate hand-off: an attempt that produced an
answer is written as ANSWERED, and only the grader promotes it to CORRECT. That keeps the
reward function in exactly one place.

The artifact is rewritten after every step and on every phase change, so a killed attempt
still yields the trajectory up to the kill, and a UI can watch progress live -- including
the ~2 minutes of provisioning before the agent starts.
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

# Modules for behaviour, so call sites say where it lives -- grader.grade() is pure, while
# grade_attempt() below also mutates status and writes to disk. Types stay direct.
from . import grader, gym_stack, vendor
from .agents.base import AgentRunResult
from .schemas import (
    AgentConfig,
    AttemptArtifact,
    AttemptPhase,
    AttemptStatus,
    EvaluationResult,
    Limits,
    StepRecord,
    StopReason,
    Task,
)

# Extra wall-clock the parent allows beyond the agent's own soft deadline, so a clean
# shutdown (closing the context, flushing the video) can finish before the SIGKILL lands.
SHUTDOWN_GRACE_S = 45

ARTIFACT_NAME = "artifact.json"
EVALUATION_NAME = "evaluation.json"
SPEC_NAME = "spec.json"
WORKER_LOG = "worker.log"
STEPS_DIR = "steps"
VIDEO_DIR = "video"

# How an attempt's reason for stopping becomes its status.
STOP_REASON_TO_STATUS: dict[StopReason, AttemptStatus] = {
    # An answer exists but has not been graded yet; grade_attempt promotes it to CORRECT.
    StopReason.SUBMITTED: AttemptStatus.ANSWERED,
    # Had budget, produced no answer. Genuine agent failure.
    StopReason.NO_FUNCTION_CALL: AttemptStatus.ANSWERED,
    StopReason.MAX_STEPS: AttemptStatus.MAX_STEPS_EXCEEDED,
    StopReason.TIMEOUT: AttemptStatus.TIMED_OUT,
    # Outcome never observed -> reward None, excluded from pass@k.
    StopReason.API_ERROR: AttemptStatus.NOT_OBSERVED,
    StopReason.HARNESS_ERROR: AttemptStatus.NOT_OBSERVED,
    # A provider-side refusal also means we failed to observe the agent's capability.
    StopReason.SAFETY_BLOCKED: AttemptStatus.NOT_OBSERVED,
    # Raised in the parent, before any worker exists.
    StopReason.GYM_PROVISION_FAILED: AttemptStatus.NOT_OBSERVED,
    StopReason.GYM_AUTH_FAILED: AttemptStatus.NOT_OBSERVED,
}

STORAGE_STATE_NAME = "storage_state.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_attempt_id(task_id: str, attempt_index: int) -> str:
    return f"{task_id}-a{attempt_index}-{uuid.uuid4().hex[:8]}"


def attempt_dir(attempt_id: str, job_id: str | None = None) -> pathlib.Path:
    return vendor.RUNS_DIR / (job_id or "adhoc") / attempt_id


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
    load_dotenv(vendor.PROJECT_ROOT / ".env")

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
        # Reaching the worker at all proves provisioning and auth already succeeded.
        phase=AttemptPhase.CONNECTING,
        pristine_counts=spec.get("pristine_counts"),
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
        artifact.status = AttemptStatus.NOT_OBSERVED
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
        artifact.status = AttemptStatus.NOT_OBSERVED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        artifact.error = f"import failed: {type(exc).__name__}: {exc}"
        finalize()
        return 1

    steps_dir = out_dir / STEPS_DIR
    video_dir = (out_dir / VIDEO_DIR) if record_video else None
    # Minted by the parent for this attempt's own gym -- no longer a shared global path.
    raw_state = spec.get("storage_state")
    storage_state = pathlib.Path(raw_state) if raw_state else None

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
            if storage_state is None or not storage_state.is_file():
                artifact.status = AttemptStatus.NOT_OBSERVED
                artifact.stop_reason = StopReason.HARNESS_ERROR
                artifact.error = f"spec carried no usable storage_state ({raw_state!r})"
                finalize()
                return 1
            if not computer.is_authenticated():
                # The parent already verified this session over HTTP, so failing here means
                # the browser could not use it -- cookie shape or origin mismatch.
                artifact.status = AttemptStatus.NOT_OBSERVED
                artifact.stop_reason = StopReason.HARNESS_ERROR
                artifact.error = (
                    "browser landed on /auth/login despite a session the parent verified"
                )
                finalize()
                return 1

            agent = GeminiComputerUseAgent(agent_config, api_key=api_key)
            artifact.phase = AttemptPhase.RUNNING
            write_artifact(artifact, out_dir)

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
        artifact.status = AttemptStatus.NOT_OBSERVED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        artifact.error = f"{type(exc).__name__}: {exc}"
        finalize(result)
        return 1

    artifact.stop_reason = result.stop_reason
    artifact.status = STOP_REASON_TO_STATUS.get(
        result.stop_reason, AttemptStatus.NOT_OBSERVED
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
    job_id: str | None = None,
    attempt_index: int = 0,
    headless: bool = True,
    record_video: bool = True,
) -> AttemptArtifact:
    """Provision a private gym, run one attempt in a subprocess, tear the gym down.

    The gym wraps the `Popen` **in this process**, never inside the worker: a timeout kills
    the worker with SIGKILL, which runs no `finally`, so teardown placed in the worker would
    leak a container and a port on exactly the path that needs cleaning up most. It also
    keeps `limits.timeout_s` a pure agent budget -- provisioning is not charged to it.
    """
    agent_config = agent_config or AgentConfig()
    limits = limits or Limits(
        max_steps=task.max_steps or Limits().max_steps,
        timeout_s=task.timeout_s or Limits().timeout_s,
    )

    attempt_id = new_attempt_id(task.id, attempt_index)
    out_dir = attempt_dir(attempt_id, job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Written before anything slow happens, so the attempt is visible in the UI from the
    # first second rather than only once the worker starts ~2 minutes later.
    pending = AttemptArtifact(
        attempt_id=attempt_id,
        task_id=task.id,
        job_id=job_id,
        attempt_index=attempt_index,
        agent_config=agent_config,
        limits=limits,
        gym_url="",
        status=AttemptStatus.IN_PROGRESS,
        phase=AttemptPhase.QUEUED,
        started_at=_now(),
    )
    write_artifact(pending, out_dir)

    def on_phase(phase: AttemptPhase) -> None:
        pending.phase = phase
        write_artifact(pending, out_dir)

    def on_pristine(counts: dict[str, int]) -> None:
        pending.pristine_counts = counts

    def fail(reason: StopReason, message: str) -> AttemptArtifact:
        pending.status = AttemptStatus.NOT_OBSERVED
        pending.stop_reason = reason
        pending.error = message
        pending.ended_at = _now()
        if pending.started_at:
            pending.duration_s = (pending.ended_at - pending.started_at).total_seconds()
        write_artifact(pending, out_dir)
        return pending

    storage_state_path = out_dir / STORAGE_STATE_NAME
    timed_out = False
    proc = None

    try:
        with gym_stack.ephemeral_gym(
            attempt_id,
            storage_state_path=storage_state_path,
            on_phase=on_phase,
            on_pristine=on_pristine,
        ) as (gym_url, storage_state):
            pending.gym_url = gym_url
            write_artifact(pending, out_dir)

            spec = {
                "attempt_id": attempt_id,
                "job_id": job_id,
                "attempt_index": attempt_index,
                "task": task.model_dump(),
                "agent_config": agent_config.model_dump(),
                "limits": limits.model_dump(),
                "out_dir": str(out_dir),
                "gym_url": gym_url,
                "storage_state": str(storage_state),
                "headless": headless,
                "record_video": record_video,
                "pristine_counts": pending.pristine_counts,
            }
            spec_path = out_dir / SPEC_NAME
            spec_path.write_text(json.dumps(spec, indent=2))

            cmd = [sys.executable, "-m", "rlgym.executor", "--worker", str(spec_path)]
            log_path = out_dir / WORKER_LOG

            with log_path.open("w") as log:
                # start_new_session gives the worker its own process group, so a timeout can
                # kill the browser and driver it spawned rather than orphaning them.
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(vendor.PROJECT_ROOT),
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
    except gym_stack.GymProvisionError as exc:
        return fail(StopReason.GYM_PROVISION_FAILED, str(exc))
    except gym_stack.GymAuthError as exc:
        return fail(StopReason.GYM_AUTH_FAILED, str(exc))

    artifact = read_artifact(out_dir)
    if artifact is None:
        # The worker died before writing anything -- an import error or an OOM kill.
        tail = ""
        log_path = out_dir / WORKER_LOG
        if log_path.is_file():
            tail = log_path.read_text()[-1500:]
        exit_code = proc.returncode if proc is not None else "n/a"
        return fail(
            StopReason.HARNESS_ERROR,
            f"worker produced no artifact (exit {exit_code}). Log tail:\n{tail}",
        )

    # The worker rebuilt the artifact from its spec, so parent-only fields are restored here.
    artifact.pristine_counts = artifact.pristine_counts or pending.pristine_counts

    if timed_out:
        # Hard kill: the worker never got to classify itself. Its partial trajectory
        # survives because the artifact is rewritten every step.
        artifact.status = AttemptStatus.TIMED_OUT
        artifact.stop_reason = StopReason.TIMEOUT
        artifact.error = (
            artifact.error
            or f"killed after {limits.timeout_s + SHUTDOWN_GRACE_S}s wall clock"
        )
    elif artifact.status is AttemptStatus.IN_PROGRESS:
        # Exited without finalizing: a crash the worker's own handler didn't catch.
        artifact.status = AttemptStatus.NOT_OBSERVED
        artifact.stop_reason = StopReason.HARNESS_ERROR
        exit_code = proc.returncode if proc is not None else "n/a"
        artifact.error = artifact.error or f"worker exited with code {exit_code} mid-attempt"

    artifact.phase = AttemptPhase.GRADING
    write_artifact(artifact, out_dir)
    return artifact


def grade_attempt(artifact: AttemptArtifact, task: Task) -> EvaluationResult:
    """Grade an attempt, promote a correct one, and persist both records.

    Kept next to the executor so `run_one` and the job runner share identical semantics:
    the executor never decides correctness, and the grader is the only thing that can set
    COMPLETED_CORRECT.
    """
    out_dir = attempt_dir(artifact.attempt_id, artifact.job_id)
    try:
        evaluation = grader.grade(artifact, task)
    except Exception as exc:  # noqa: BLE001
        # Contained deliberately: this runs inside a pool future, so an exception here
        # propagates through future.result() and marks the WHOLE job FAILED. One malformed
        # task.answer would take down a 30-attempt run.
        evaluation = EvaluationResult(
            attempt_id=artifact.attempt_id,
            task_id=task.id,
            verdict="not_graded",
            reward=None,
            diff=f"grader raised {type(exc).__name__}: {exc}",
            evaluated_at=_now(),
            evaluator_version=grader.EVALUATOR_VERSION,
        )
    else:
        if evaluation.verdict == "pass":
            artifact.status = AttemptStatus.CORRECT

    artifact.phase = AttemptPhase.DONE
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
