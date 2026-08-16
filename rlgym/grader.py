"""Reward function: compare a submitted answer against a task's expected output.

Kept a pure function of (artifact, task) so stored trajectories can be re-graded later
without re-running any rollouts. When the rules here change, bump EVALUATOR_VERSION -- old
verdicts stay attributable to the rules that produced them.

Three comparison problems this has to solve, all of them real in tasks.json:

1. **Formatting.** Metabase renders `101203.3` as `101,203.3`, and a model may echo `5` as
   `5.0`. Compared numerically with tolerance, not as strings.

2. **List order.** `problem9` asks for a set of names where order carries no meaning, so a
   correct answer in a different order must pass. Lists compare as multisets.

3. **Pairing (the subtle one).** `problem1` and `problem2` return two parallel lists where
   element *i* of each corresponds -- titles and their ratings. Comparing the two lists
   independently as multisets would accept an answer that has all the right titles and all
   the right ratings but pairs them wrongly. Equal-length list values are therefore zipped
   into tuples and the tuples compared, which keeps each value bound to its label. It also
   makes `problem2`'s ranking survive implicitly, since each count travels with its title.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

# Module import for behaviour (schemas.reward_for_status), direct import for the types
# used in annotations -- qualifying those adds length without adding information.
from . import schemas
from .schemas import AttemptArtifact, AttemptStatus, EvaluationResult, Task

EVALUATOR_VERSION = "1.0.0"

REL_TOL = 1e-6
ABS_TOL = 1e-2

# A mismatch within this relative distance is reported as a near miss -- structurally
# right, numerically just outside tolerance. Worth a human look rather than being trusted
# as a clean agent failure (e.g. a value read off a rounded chart label).
NEAR_MISS_REL = 0.05


class ComparisonOutcome(BaseModel):
    matched: bool
    diff: str | None = None
    near_miss: bool = False


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------


def _normalize_scalar(value: Any) -> Any:
    """Canonical form for one leaf value.

    Numeric-looking strings become floats so `"101,203.30"` and `101203.3` compare equal.
    Everything else becomes whitespace-collapsed casefolded text.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        cleaned = text.replace(",", "").replace("$", "").replace("%", "").strip()
        # Only treat it as a number if the WHOLE string is one -- "Q4" and product titles
        # must stay text.
        try:
            return float(cleaned)
        except ValueError:
            return " ".join(text.split()).casefold()
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_norm_key(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return _normalize_scalar(value)


def _norm_key(key: Any) -> str:
    return " ".join(str(key).strip().split()).casefold()


# --------------------------------------------------------------------------------------
# Leaf comparison
# --------------------------------------------------------------------------------------


def _scalars_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, float) and isinstance(actual, float):
        return math.isclose(expected, actual, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return expected == actual


def _scalars_near(expected: Any, actual: Any) -> bool:
    """Close enough to flag for review, but not a pass."""
    if isinstance(expected, float) and isinstance(actual, float):
        if expected == 0:
            return abs(actual) <= NEAR_MISS_REL
        return abs(expected - actual) / abs(expected) <= NEAR_MISS_REL
    if isinstance(expected, str) and isinstance(actual, str):
        # "q4" vs "q4 2025" -- the model answered correctly but over-specified.
        return expected in actual or actual in expected
    return False


def _items_equal(expected: Any, actual: Any) -> bool:
    """Compare list elements, which may be scalars or zipped tuples."""
    if isinstance(expected, tuple) and isinstance(actual, tuple):
        return len(expected) == len(actual) and all(
            _scalars_equal(e, a) for e, a in zip(expected, actual)
        )
    if isinstance(expected, (list, dict)) or isinstance(actual, (list, dict)):
        return _compare(expected, actual).matched
    return _scalars_equal(expected, actual)


def _multiset_equal(expected: list[Any], actual: list[Any]) -> bool:
    """Order-insensitive comparison with tolerance.

    collections.Counter can't be used: float tolerance means equality isn't hashable-
    equivalent, so this greedily matches each expected item against a remaining actual.
    """
    if len(expected) != len(actual):
        return False
    remaining = list(actual)
    for want in expected:
        for i, have in enumerate(remaining):
            if _items_equal(want, have):
                remaining.pop(i)
                break
        else:
            return False
    return not remaining


# --------------------------------------------------------------------------------------
# Structural comparison
# --------------------------------------------------------------------------------------


def _parallel_list_keys(mapping: dict[str, Any]) -> list[str]:
    """Keys whose values are equal-length lists, when there are at least two of them.

    That is the signature of parallel arrays (titles + ratings). A single list key is not
    parallel -- it's just a set of values.
    """
    list_keys = [k for k, v in mapping.items() if isinstance(v, list)]
    if len(list_keys) < 2:
        return []
    lengths = {len(mapping[k]) for k in list_keys}
    if len(lengths) != 1:
        # Ragged lists aren't parallel arrays; compare them independently instead.
        return []
    return sorted(list_keys)


def _compare(expected: Any, actual: Any) -> ComparisonOutcome:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return ComparisonOutcome(
                matched=False,
                diff=f"expected an object, got {type(actual).__name__}",
            )

        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing key(s): {', '.join(missing)}")
            if extra:
                parts.append(f"unexpected key(s): {', '.join(extra)}")
            return ComparisonOutcome(matched=False, diff="; ".join(parts))

        problems: list[str] = []
        near = True  # stays true only if every problem is a near miss

        parallel = _parallel_list_keys(expected)
        if parallel and _parallel_list_keys(actual) == parallel:
            # Bind each value to its label before comparing, so pairing is enforced.
            want_rows = [tuple(expected[k][i] for k in parallel) for i in range(len(expected[parallel[0]]))]
            have_rows = [tuple(actual[k][i] for k in parallel) for i in range(len(actual[parallel[0]]))]
            if not _multiset_equal(want_rows, have_rows):
                problems.append(
                    f"rows for ({', '.join(parallel)}) do not match: "
                    f"expected {sorted(map(str, want_rows))}, got {sorted(map(str, have_rows))}"
                )
                near = False
            compare_keys = [k for k in expected if k not in parallel]
        else:
            if parallel:
                problems.append(
                    f"expected parallel lists ({', '.join(parallel)}) of equal length; "
                    "answer's lists are ragged or mismatched"
                )
                near = False
            compare_keys = list(expected)

        for key in compare_keys:
            outcome = _compare(expected[key], actual[key])
            if not outcome.matched:
                problems.append(f"{key}: {outcome.diff}")
                near = near and outcome.near_miss

        if problems:
            return ComparisonOutcome(
                matched=False, diff="; ".join(problems), near_miss=near
            )
        return ComparisonOutcome(matched=True)

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return ComparisonOutcome(
                matched=False, diff=f"expected a list, got {type(actual).__name__}"
            )
        if len(expected) != len(actual):
            return ComparisonOutcome(
                matched=False,
                diff=f"expected {len(expected)} item(s), got {len(actual)}",
            )
        if not _multiset_equal(expected, actual):
            return ComparisonOutcome(
                matched=False,
                diff=f"expected {sorted(map(str, expected))}, got {sorted(map(str, actual))}",
            )
        return ComparisonOutcome(matched=True)

    if _scalars_equal(expected, actual):
        return ComparisonOutcome(matched=True)
    return ComparisonOutcome(
        matched=False,
        diff=f"expected {expected!r}, got {actual!r}",
        near_miss=_scalars_near(expected, actual),
    )


def compare_outputs(expected: Any, actual: Any) -> ComparisonOutcome:
    """Normalize both sides, then compare. Public entry point for tests."""
    return _compare(_normalize(expected), _normalize(actual))


# --------------------------------------------------------------------------------------
# Grading an attempt
# --------------------------------------------------------------------------------------


def grade(artifact: AttemptArtifact, task: Task) -> EvaluationResult:
    """Produce a verdict for one attempt.

    Only COMPLETED_* attempts are compared. Every other terminal status already carries
    its reward: MAX_STEPS_EXCEEDED and TIMED_OUT are agent failures (0), NOT_OBSERVED means the
    outcome was never observed (None) and must not be scored as a failure.
    """
    now = datetime.now(timezone.utc)
    expected = task.expected_output()

    def result(
        verdict: str,
        reward: int | None,
        diff: str | None = None,
        near_miss: bool = False,
    ) -> EvaluationResult:
        return EvaluationResult(
            attempt_id=artifact.attempt_id,
            task_id=task.id,
            expected_output=expected,
            actual_output=artifact.actual_output,
            verdict=verdict,  # type: ignore[arg-type]
            reward=reward,
            diff=diff,
            near_miss=near_miss,
            evaluated_at=now,
            evaluator_version=EVALUATOR_VERSION,
        )

    if artifact.status not in schemas.GRADEABLE_STATUSES:
        reward = schemas.reward_for_status(artifact.status)
        if artifact.status is AttemptStatus.NOT_OBSERVED:
            return result(
                "not_graded",
                None,
                diff=f"not graded -- outcome not observed: {artifact.error or 'unknown'}",
            )
        return result(
            "fail", reward, diff=f"not graded -- status {artifact.status.value}"
        )

    if artifact.actual_output is None:
        if artifact.actual_output_raw:
            return result(
                "fail",
                0,
                diff="submitted answer was not valid JSON: "
                f"{artifact.actual_output_raw[:200]!r}",
            )
        return result(
            "fail", 0, diff="agent finished without calling submit_answer"
        )

    outcome = compare_outputs(expected, artifact.actual_output)
    if outcome.matched:
        return result("pass", 1)
    return result("fail", 0, diff=outcome.diff, near_miss=outcome.near_miss)
