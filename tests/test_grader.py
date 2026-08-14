"""Grader tests.

The grader is the reward function, so a bug here silently mislabels every rollout. These
tests do not depend on the agent, the gym, or whether tasks.json's answers match our
image's sample data -- they only exercise the comparison logic.

Run:  agent/.venv/bin/python -m pytest tests/ -v
"""

from __future__ import annotations

import copy

import pytest

from rlgym.grader import compare_outputs, grade
from rlgym.schemas import (
    AgentConfig,
    AttemptArtifact,
    AttemptStatus,
    Limits,
    StopReason,
    Task,
)
from rlgym.tasks import load_task_map

TASKS = load_task_map()


def make_artifact(
    task_id: str,
    actual_output=None,
    status: AttemptStatus = AttemptStatus.COMPLETED_INCORRECT,
    actual_output_raw: str | None = None,
    error: str | None = None,
) -> AttemptArtifact:
    return AttemptArtifact(
        attempt_id=f"test-{task_id}",
        task_id=task_id,
        agent_config=AgentConfig(),
        limits=Limits(),
        gym_url="http://localhost:3000",
        status=status,
        stop_reason=StopReason.SUBMITTED,
        actual_output=actual_output,
        actual_output_raw=actual_output_raw,
        error=error,
    )


# ----------------------------------------------------------------------------------------
# The baseline: every expected answer must grade itself as correct
# ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_expected_answer_grades_pass(task_id: str) -> None:
    task = TASKS[task_id]
    artifact = make_artifact(task_id, actual_output=task.expected_output())
    result = grade(artifact, task)
    assert result.verdict == "pass", result.diff
    assert result.reward == 1


# ----------------------------------------------------------------------------------------
# Pairing -- the failure mode independent list comparison would miss
# ----------------------------------------------------------------------------------------


def test_wrong_pairing_fails() -> None:
    """All the right titles and all the right ratings, paired wrongly, must FAIL."""
    task = TASKS["problem1"]
    answer = copy.deepcopy(task.expected_output())
    # Same multiset of ratings, same titles -- only the correspondence is broken.
    answer["ratings"] = [5, 4.6, 5]
    assert answer["ratings"] != task.expected_output()["ratings"]
    assert sorted(answer["ratings"]) == sorted(task.expected_output()["ratings"])

    result = grade(make_artifact("problem1", answer), task)
    assert result.verdict == "fail"
    assert "do not match" in (result.diff or "")


def test_parallel_rows_reordered_together_passes() -> None:
    """Reordering both lists in step preserves pairing, so it must PASS."""
    task = TASKS["problem2"]
    expected = task.expected_output()
    order = [2, 0, 1]
    answer = {
        "product_titles": [expected["product_titles"][i] for i in order],
        "order_counts": [expected["order_counts"][i] for i in order],
    }
    result = grade(make_artifact("problem2", answer), task)
    assert result.verdict == "pass", result.diff


def test_single_list_order_does_not_matter() -> None:
    """problem9 is a set of names -- order carries no meaning."""
    task = TASKS["problem9"]
    answer = {"names": list(reversed(task.expected_output()["names"]))}
    result = grade(make_artifact("problem9", answer), task)
    assert result.verdict == "pass", result.diff


# ----------------------------------------------------------------------------------------
# Formatting tolerance
# ----------------------------------------------------------------------------------------


def test_thousands_separator_passes() -> None:
    """Metabase renders 101203.3 as '101,203.30'."""
    task = TASKS["problem8"]
    answer = dict(task.expected_output())
    answer["sum"] = "101,203.30"
    result = grade(make_artifact("problem8", answer), task)
    assert result.verdict == "pass", result.diff


def test_int_float_equivalence_passes() -> None:
    """problem1's expected ratings mix 4.6 and int 5."""
    task = TASKS["problem1"]
    answer = copy.deepcopy(task.expected_output())
    answer["ratings"] = [4.6, 5.0, 5.0]
    assert grade(make_artifact("problem1", answer), task).verdict == "pass"


def test_string_case_and_whitespace_tolerated() -> None:
    task = TASKS["problem3"]
    answer = {k: f"  {v.upper()}  " for k, v in task.expected_output().items()}
    result = grade(make_artifact("problem3", answer), task)
    assert result.verdict == "pass", result.diff


def test_float_rounding_within_tolerance_passes() -> None:
    task = TASKS["problem10"]
    answer = {"avg_rating": 3.1500001}
    assert grade(make_artifact("problem10", answer), task).verdict == "pass"


# ----------------------------------------------------------------------------------------
# Genuine failures
# ----------------------------------------------------------------------------------------


def test_wrong_number_fails() -> None:
    task = TASKS["problem10"]
    result = grade(make_artifact("problem10", {"avg_rating": 4.2}), task)
    assert result.verdict == "fail"
    assert result.reward == 0


def test_missing_key_fails() -> None:
    task = TASKS["problem5"]
    answer = task.expected_output()
    answer.pop("rating")
    result = grade(make_artifact("problem5", answer), task)
    assert result.verdict == "fail"
    assert "missing key" in (result.diff or "")


def test_extra_key_fails() -> None:
    task = TASKS["problem10"]
    result = grade(
        make_artifact("problem10", {"avg_rating": 3.15, "extra": 1}), task
    )
    assert result.verdict == "fail"
    assert "unexpected key" in (result.diff or "")


def test_wrong_list_length_fails() -> None:
    task = TASKS["problem9"]
    answer = {"names": task.expected_output()["names"][:2]}
    result = grade(make_artifact("problem9", answer), task)
    assert result.verdict == "fail"


# ----------------------------------------------------------------------------------------
# Near miss -- structurally right, semantically over-specified
# ----------------------------------------------------------------------------------------


def test_over_specified_string_is_near_miss() -> None:
    """problem6 wants 'Q4'; a model may well answer 'Q4 2025'.

    Still a fail, but flagged so it's reviewable rather than trusted as agent failure.
    """
    task = TASKS["problem6"]
    answer = dict(task.expected_output())
    answer["quarter"] = "Q4 2025"
    result = grade(make_artifact("problem6", answer), task)
    assert result.verdict == "fail"
    assert result.near_miss is True


def test_slightly_off_number_is_near_miss() -> None:
    """A value read off a rounded chart label."""
    task = TASKS["problem10"]
    result = grade(make_artifact("problem10", {"avg_rating": 3.2}), task)
    assert result.verdict == "fail"
    assert result.near_miss is True


def test_badly_wrong_number_is_not_near_miss() -> None:
    task = TASKS["problem10"]
    result = grade(make_artifact("problem10", {"avg_rating": 1.0}), task)
    assert result.near_miss is False


# ----------------------------------------------------------------------------------------
# Non-gradeable statuses -- the part that protects the dataset
# ----------------------------------------------------------------------------------------


def test_errored_is_not_graded_and_reward_is_none() -> None:
    """ERRORED must NOT become reward 0 -- the outcome was never observed."""
    task = TASKS["problem1"]
    artifact = make_artifact(
        "problem1", status=AttemptStatus.ERRORED, error="Gemini 503"
    )
    result = grade(artifact, task)
    assert result.verdict == "not_graded"
    assert result.reward is None
    assert artifact.reward is None


@pytest.mark.parametrize(
    "status", [AttemptStatus.MAX_STEPS_EXCEEDED, AttemptStatus.TIMED_OUT]
)
def test_budget_exhaustion_is_reward_zero(status: AttemptStatus) -> None:
    """Running out of budget IS agent failure, unlike ERRORED."""
    task = TASKS["problem1"]
    artifact = make_artifact("problem1", status=status)
    result = grade(artifact, task)
    assert result.verdict == "fail"
    assert result.reward == 0
    assert artifact.reward == 0


def test_no_answer_submitted_fails() -> None:
    task = TASKS["problem1"]
    artifact = make_artifact("problem1", actual_output=None)
    result = grade(artifact, task)
    assert result.verdict == "fail"
    assert result.reward == 0
    assert "without calling submit_answer" in (result.diff or "")


def test_unparseable_answer_reports_the_raw_text() -> None:
    task = TASKS["problem1"]
    artifact = make_artifact(
        "problem1", actual_output=None, actual_output_raw="here you go: {oops"
    )
    result = grade(artifact, task)
    assert result.verdict == "fail"
    assert "not valid JSON" in (result.diff or "")


# ----------------------------------------------------------------------------------------
# Shape mismatches
# ----------------------------------------------------------------------------------------


def test_answer_of_wrong_type_fails_cleanly() -> None:
    task = TASKS["problem10"]
    result = grade(make_artifact("problem10", ["not", "a", "dict"]), task)
    assert result.verdict == "fail"
    assert "expected an object" in (result.diff or "")


def test_compare_outputs_is_usable_standalone() -> None:
    assert compare_outputs({"a": 1}, {"a": 1.0}).matched
    assert not compare_outputs({"a": 1}, {"a": 2}).matched
