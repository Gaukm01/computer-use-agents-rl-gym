"""Orchestration and pass@k tests.

The executor is stubbed out, so these run in milliseconds with no API calls and no
browser. What's under test is the batching, aggregation and resume logic -- and above all
that ERRORED attempts are excluded from pass@k rather than counted as failures, which is
the property that keeps a job's headline number honest.

Stubbing works cleanly because the orchestrator calls `executor.execute_attempt()` through
the module rather than a name imported at module load, so monkeypatching the attribute is
enough. A nice side effect of the module-import convention.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from rlgym import executor, orchestrator, service, vendor
from rlgym.schemas import (
    AgentConfig,
    AttemptArtifact,
    AttemptPhase,
    AttemptStatus,
    EvaluationResult,
    JobStatus,
    Limits,
    StopReason,
    TaskResult,
    AttemptSummary,
)

TASK_IDS = ["problem10", "problem5", "problem6"]


@pytest.fixture(autouse=True)
def isolated_runs(tmp_path, monkeypatch):
    """Point every runs/ path at a temp dir so tests never touch real artifacts."""
    monkeypatch.setattr(vendor, "RUNS_DIR", tmp_path / "runs")
    return tmp_path


def make_artifact(task_id, attempt_index, job_id, status) -> AttemptArtifact:
    return AttemptArtifact(
        attempt_id=f"{task_id}-a{attempt_index}-stub",
        task_id=task_id,
        job_id=job_id,
        attempt_index=attempt_index,
        agent_config=AgentConfig(),
        limits=Limits(),
        gym_url="http://localhost:3000",
        status=status,
        stop_reason=StopReason.SUBMITTED,
        step_count=3,
        duration_s=1.0,
    )


def stub_executor(monkeypatch, outcomes, record=None, barrier=None):
    """Replace the executor with a deterministic stub.

    `outcomes` maps (task_id, attempt_index) -> AttemptStatus, defaulting to
    COMPLETED_INCORRECT for anything unlisted.
    """
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_execute(task, *, job_id, attempt_index, **kwargs):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        if record is not None:
            record.append((task.id, attempt_index))
        if barrier is not None:
            barrier()
        status = outcomes.get(
            (task.id, attempt_index), AttemptStatus.ANSWERED
        )
        with lock:
            live["now"] -= 1
        return make_artifact(task.id, attempt_index, job_id, status)

    def fake_grade(artifact, task):
        return EvaluationResult(
            attempt_id=artifact.attempt_id,
            task_id=task.id,
            verdict="pass"
            if artifact.status is AttemptStatus.CORRECT
            else "not_graded"
            if artifact.status is AttemptStatus.NOT_OBSERVED
            else "fail",
            reward=artifact.reward,
            evaluated_at=datetime.now(timezone.utc),
            evaluator_version="test",
        )

    monkeypatch.setattr(executor, "execute_attempt", fake_execute)
    monkeypatch.setattr(executor, "grade_attempt", fake_grade)
    return live


# ----------------------------------------------------------------------------------------
# pass@k arithmetic (pure, no orchestration)
# ----------------------------------------------------------------------------------------


def summary(index: int, reward: int | None) -> AttemptSummary:
    status = {
        1: AttemptStatus.CORRECT,
        0: AttemptStatus.ANSWERED,
        None: AttemptStatus.NOT_OBSERVED,
    }[reward]
    return AttemptSummary(
        attempt_id=f"a{index}",
        task_id="t",
        attempt_index=index,
        status=status,
        reward=reward,
    )


def test_pass_at_k_is_any_valid_success() -> None:
    task = TaskResult(task_id="t", attempts=[summary(0, 0), summary(1, 1), summary(2, 0)])
    task.recompute(requested_attempts=3)
    assert task.pass_at_k is True
    assert (task.passed, task.failed, task.errored) == (1, 2, 0)
    assert task.pass_rate == pytest.approx(1 / 3)


def test_errored_attempts_leave_the_denominator() -> None:
    """The core guarantee: 1 pass + 1 fail + 1 errored is a 50% rate, not 33%."""
    task = TaskResult(
        task_id="t", attempts=[summary(0, 1), summary(1, 0), summary(2, None)]
    )
    task.recompute(requested_attempts=3)
    assert task.valid_attempts == 2
    assert task.pass_rate == pytest.approx(0.5)
    assert task.errored == 1


def test_all_errored_is_ungraded_not_failed() -> None:
    task = TaskResult(task_id="t", attempts=[summary(0, None), summary(1, None)])
    task.recompute(requested_attempts=2)
    assert task.valid_attempts == 0
    assert task.pass_rate is None
    assert task.pass_at_k is False


def test_full_k_with_no_errors_is_confident() -> None:
    task = TaskResult(task_id="t", attempts=[summary(0, 0), summary(1, 0)])
    task.recompute(requested_attempts=2)
    assert task.pass_rate == 0.0


# ----------------------------------------------------------------------------------------
# End-to-end orchestration, executor stubbed
# ----------------------------------------------------------------------------------------


def test_job_runs_every_task_times_k(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    stub_executor(monkeypatch, {}, record=calls)

    job = service.submit_job(TASK_IDS, attempts_per_task=2, concurrency=2, block=True)

    assert job.status is JobStatus.COMPLETED
    assert len(calls) == len(TASK_IDS) * 2
    assert job.completed_attempts == 6
    assert {t.task_id for t in job.tasks} == set(TASK_IDS)
    assert all(t.total_attempts == 2 for t in job.tasks)


def test_attempts_are_ordered_task_major(monkeypatch) -> None:
    """A task's K attempts run together, so its pass@k is final as early as possible.
    A half-finished job then has complete, usable results for half the tasks rather than
    one sample of every task and nothing settled."""
    calls: list[tuple[str, int]] = []
    # concurrency=1 makes submission order observable.
    stub_executor(monkeypatch, {}, record=calls)

    service.submit_job(TASK_IDS, attempts_per_task=2, concurrency=1, block=True)

    assert calls == [(task_id, index) for task_id in TASK_IDS for index in (0, 1)]


def test_concurrency_is_respected(monkeypatch) -> None:
    gate = threading.Barrier(2, timeout=10)
    live = stub_executor(monkeypatch, {}, barrier=lambda: gate.wait())

    service.submit_job(TASK_IDS, attempts_per_task=2, concurrency=2, block=True)

    assert live["max"] <= 2, "ran more attempts at once than concurrency allows"
    assert live["max"] == 2, "did not actually parallelize"


def test_job_aggregates_mixed_outcomes(monkeypatch) -> None:
    stub_executor(
        monkeypatch,
        {
            ("problem10", 0): AttemptStatus.CORRECT,
            ("problem10", 1): AttemptStatus.ANSWERED,
            ("problem5", 0): AttemptStatus.NOT_OBSERVED,
            ("problem5", 1): AttemptStatus.NOT_OBSERVED,
            ("problem6", 0): AttemptStatus.MAX_STEPS_EXCEEDED,
            ("problem6", 1): AttemptStatus.TIMED_OUT,
        },
    )

    job = service.submit_job(TASK_IDS, attempts_per_task=2, concurrency=3, block=True)
    by_id = {t.task_id: t for t in job.tasks}

    assert by_id["problem10"].pass_at_k is True
    assert by_id["problem10"].pass_rate == pytest.approx(0.5)

    # Entirely unobserved -- must not be reported as a failing task.
    assert by_id["problem5"].valid_attempts == 0
    assert by_id["problem5"].pass_rate is None

    # Budget exhaustion IS agent failure, unlike ERRORED.
    assert by_id["problem6"].failed == 2
    assert by_id["problem6"].errored == 0

    assert (job.passed, job.failed, job.errored) == (1, 3, 2)
    assert job.valid_attempts == 4
    # problem5 is ungraded, so pass@k averages over the two graded tasks only.
    assert job.pass_at_k_rate == pytest.approx(0.5)
    assert job.mean_reward == pytest.approx(0.25)


def test_resume_skips_completed_attempts(monkeypatch) -> None:
    """Re-running a job must not repeat finished work."""
    calls: list[tuple[str, int]] = []
    stub_executor(monkeypatch, {}, record=calls)

    job = service.submit_job(TASK_IDS, attempts_per_task=1, concurrency=3, block=True)
    assert len(calls) == 3

    # The stub never wrote artifact.json, so simulate what a real run leaves behind.
    for task_id in TASK_IDS:
        artifact = make_artifact(task_id, 0, job.spec.job_id, AttemptStatus.CORRECT)
        executor.write_artifact(
            artifact, executor.attempt_dir(artifact.attempt_id, job.spec.job_id)
        )

    calls.clear()
    resumed = service.resume_job(job.spec.job_id, block=True)

    assert calls == [], "resume re-ran attempts that were already terminal"
    assert resumed.completed_attempts == 3
    assert resumed.passed == 3


def test_cancel_stops_scheduling(monkeypatch) -> None:
    stub_executor(monkeypatch, {})
    cancel = threading.Event()
    cancel.set()  # cancelled before it starts

    spec = orchestrator.build_spec(TASK_IDS, attempts_per_task=2, concurrency=1)
    job = orchestrator.run_job(spec, cancel=cancel)

    assert job.status is JobStatus.CANCELLED
    assert job.completed_attempts == 0


# ----------------------------------------------------------------------------------------
# Live progress: every requested attempt is visible before it runs
# ----------------------------------------------------------------------------------------


def test_every_requested_attempt_has_a_row_from_the_start(monkeypatch) -> None:
    """The UI renders the whole T x K grid, so an attempt still in the queue needs a row
    to occupy. Cancelling before anything runs leaves exactly those placeholders."""
    stub_executor(monkeypatch, {})
    cancel = threading.Event()
    cancel.set()

    spec = orchestrator.build_spec(TASK_IDS, attempts_per_task=3, concurrency=1)
    job = orchestrator.run_job(spec, cancel=cancel)

    assert sum(len(t.attempts) for t in job.tasks) == len(TASK_IDS) * 3
    for task in job.tasks:
        assert [a.attempt_index for a in task.attempts] == [0, 1, 2]
        assert all(a.status is AttemptStatus.QUEUED for a in task.attempts)
        # No attempt directory exists yet, which is what makes the row unopenable.
        assert all(a.attempt_id == "" for a in task.attempts)


def test_unstarted_attempts_are_not_counted_as_errors(monkeypatch) -> None:
    """A queued attempt has reward None, exactly like an infrastructure failure. Counting
    them the same way would report a job as broken the moment it was submitted."""
    stub_executor(monkeypatch, {})
    cancel = threading.Event()
    cancel.set()

    spec = orchestrator.build_spec(TASK_IDS, attempts_per_task=2, concurrency=1)
    job = orchestrator.run_job(spec, cancel=cancel)

    assert job.errored == 0
    assert job.queued_attempts == len(TASK_IDS) * 2
    assert job.total_attempts == len(TASK_IDS) * 2


def write_live(job_id: str, task_id: str, index: int, *, phase, status, started=None):
    artifact = make_artifact(task_id, index, job_id, status)
    artifact.attempt_id = f"{task_id}-a{index}-{phase.value.lower()}"
    artifact.phase = phase
    artifact.started_at = started or datetime.now(timezone.utc)
    executor.write_artifact(
        artifact, executor.attempt_dir(artifact.attempt_id, job_id)
    )
    return artifact


def test_in_flight_attempts_report_their_phase() -> None:
    """Provisioning takes ~35s before the agent does anything. Without the phase an
    attempt looks hung; this is what the watcher reads off disk."""
    write_live(
        "job-x",
        "problem10",
        0,
        phase=AttemptPhase.PROVISIONING,
        status=AttemptStatus.IN_PROGRESS,
    )
    found = orchestrator.load_attempts("job-x")

    row = found[("problem10", 0)]
    assert row.status is AttemptStatus.IN_PROGRESS
    assert row.phase is AttemptPhase.PROVISIONING
    assert row.is_terminal is False
    # Still not done, so resume must not treat it as finished work.
    assert ("problem10", 0) not in orchestrator.load_completed_attempts("job-x")


def test_a_finished_attempt_beats_an_abandoned_one() -> None:
    """An interrupted run leaves its dead IN_PROGRESS artifact on disk, and the retry gets
    a fresh directory. If the stale one won, a completed slot would show as forever
    running."""
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    write_live(
        "job-y",
        "problem10",
        0,
        phase=AttemptPhase.RUNNING,
        status=AttemptStatus.IN_PROGRESS,
        started=stale,
    )
    write_live(
        "job-y",
        "problem10",
        0,
        phase=AttemptPhase.DONE,
        status=AttemptStatus.CORRECT,
    )

    row = orchestrator.load_attempts("job-y")[("problem10", 0)]
    assert row.status is AttemptStatus.CORRECT
    assert row.reward == 1


def test_run_stats_average_over_observed_attempts_only() -> None:
    """An attempt that died in provisioning contributes 0 steps and a few seconds.
    Averaging it in would make a run look cheaper and faster than it is."""
    ran = summary(0, 1)
    ran.duration_s, ran.step_count, ran.total_tokens = 300.0, 20, 400_000
    also_ran = summary(1, 0)
    also_ran.duration_s, also_ran.step_count, also_ran.total_tokens = 100.0, 10, 200_000
    died = summary(2, None)
    died.duration_s, died.step_count, died.total_tokens = 4.0, 0, 0

    task = TaskResult(task_id="t", attempts=[ran, also_ran, died])
    task.recompute(requested_attempts=3)

    assert task.stats.observed_attempts == 2
    assert task.stats.avg_duration_s == pytest.approx(200.0)
    assert task.stats.avg_steps == pytest.approx(15.0)
    # Tokens are billed whatever the outcome, so they are summed over everything.
    assert task.stats.total_tokens == 600_000


def test_job_state_is_persisted_for_polling(monkeypatch) -> None:
    """A UI follows a job by reading job.json, so it must exist and stay valid."""
    stub_executor(monkeypatch, {})
    job = service.submit_job(["problem10"], attempts_per_task=1, block=True)

    path = orchestrator.job_dir(job.spec.job_id) / orchestrator.JOB_FILE
    assert path.is_file()
    reloaded = orchestrator.read_job(job.spec.job_id)
    assert reloaded is not None
    assert reloaded.spec.job_id == job.spec.job_id
    assert reloaded.status is JobStatus.COMPLETED


def test_jobs_are_listed_newest_first(monkeypatch) -> None:
    stub_executor(monkeypatch, {})
    first = service.submit_job(["problem10"], block=True)
    second = service.submit_job(["problem5"], block=True)

    listed = [j.spec.job_id for j in service.list_jobs()]
    assert listed[:2] == [second.spec.job_id, first.spec.job_id]


# ----------------------------------------------------------------------------------------
# Service-layer guards
# ----------------------------------------------------------------------------------------


def test_unknown_task_is_rejected_before_any_work() -> None:
    with pytest.raises(KeyError, match="unknown task"):
        service.submit_job(["problem10", "does_not_exist"], block=True)


@pytest.mark.parametrize("kwargs", [{"attempts_per_task": 0}, {"concurrency": 0}])
def test_invalid_job_parameters_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        service.submit_job(["problem10"], block=True, **kwargs)


def test_default_is_one_attempt(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    stub_executor(monkeypatch, {}, record=calls)
    service.submit_job(["problem10"], block=True)
    assert len(calls) == 1


def test_no_task_ids_means_every_task(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    stub_executor(monkeypatch, {}, record=calls)
    job = service.submit_job(None, block=True)
    assert len(job.spec.task_ids) == 10
    assert len(calls) == 10


def test_attempt_file_refuses_path_traversal(monkeypatch) -> None:
    stub_executor(monkeypatch, {})
    job = service.submit_job(["problem10"], block=True)
    attempt_id = "problem10-a0-stub"
    out_dir = executor.attempt_dir(attempt_id, job.spec.job_id)
    (out_dir / executor.STEPS_DIR).mkdir(parents=True, exist_ok=True)
    (out_dir / executor.STEPS_DIR / "step_000.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    assert service.attempt_file(job.spec.job_id, attempt_id, "steps/step_000.png")
    assert service.attempt_file(job.spec.job_id, attempt_id, "../../../../etc/passwd") is None
    assert service.attempt_file(job.spec.job_id, attempt_id, "nope.png") is None
