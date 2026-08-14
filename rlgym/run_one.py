"""Run a single task once and print the result.

The demo and debug entrypoint. It is a thin wrapper over `execute_attempt()` +
`grade_attempt()` -- exactly the same functions the job runner calls -- so what you see here
is what a rollout does, minus the batching.

    # gym must be up first
    colima start && (cd metabase && docker compose up -d)
    agent/.venv/bin/python -m rlgym.capture_auth        # once, mints the auth cookie

    agent/.venv/bin/python -m rlgym.run_one --task problem10
    agent/.venv/bin/python -m rlgym.run_one --task problem1 --headed --max-steps 40
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

# Modules for behaviour, types direct.
from . import executor, tasks, vendor
from .schemas import AgentConfig, AttemptStatus, Limits

load_dotenv(vendor.PROJECT_ROOT / ".env")

# ANSI, kept trivial -- no dependency needed for four colours.
BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)

STATUS_COLOUR = {
    AttemptStatus.COMPLETED_CORRECT: GREEN,
    AttemptStatus.COMPLETED_INCORRECT: RED,
    AttemptStatus.MAX_STEPS_EXCEEDED: YELLOW,
    AttemptStatus.TIMED_OUT: YELLOW,
    AttemptStatus.ERRORED: YELLOW,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one task once against the gym.")
    parser.add_argument("--task", required=True, help="Task id, e.g. problem1")
    parser.add_argument("--tasks-file", default=None, help="Override tasks.json path")
    parser.add_argument("--gym-url", default=None, help="Default http://localhost:3000")
    parser.add_argument("--model", default=AgentConfig().model)
    parser.add_argument("--hint", default=None, help="Optional extra guidance (ablation)")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None, help="Wall-clock seconds")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window. Useful for watching; slower.",
    )
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument(
        "--temperature",
        type=float,
        default=AgentConfig().temperature,
        help="Kept >0 by default: pass@k wants sampling diversity.",
    )
    args = parser.parse_args()

    try:
        # Named task_map, not tasks -- `tasks` is the module.
        task_map = tasks.load_task_map(args.tasks_file)
    except Exception as exc:
        print(f"{RED}failed to load tasks: {exc}{RESET}")
        return 2

    if args.task not in task_map:
        print(f"{RED}unknown task {args.task!r}{RESET}")
        print(f"available: {', '.join(task_map)}")
        return 2
    task = task_map[args.task]

    defaults = Limits()
    limits = Limits(
        max_steps=args.max_steps or task.max_steps or defaults.max_steps,
        timeout_s=args.timeout or task.timeout_s or defaults.timeout_s,
    )
    agent_config = AgentConfig(
        model=args.model, hint=args.hint, temperature=args.temperature
    )

    print(f"{BOLD}task{RESET}    {task.id}")
    print(f"{BOLD}prompt{RESET}  {task.task}")
    print(f"{BOLD}budget{RESET}  {limits.max_steps} steps / {limits.timeout_s}s")
    print(f"{BOLD}model{RESET}   {agent_config.model}")
    print(f"{DIM}running (headless={not args.headed}) ...{RESET}\n")

    artifact = executor.execute_attempt(
        task,
        agent_config=agent_config,
        limits=limits,
        gym_url=args.gym_url,
        headless=not args.headed,
        record_video=not args.no_video,
    )
    # executor.grade_attempt, not grader.grade: this one also promotes the status on a
    # pass and persists both records.
    evaluation = executor.grade_attempt(artifact, task)

    colour = STATUS_COLOUR.get(artifact.status, "")
    reward = artifact.reward
    print(f"{BOLD}status{RESET}      {colour}{artifact.status.value}{RESET}")
    print(f"{BOLD}stop{RESET}        {artifact.stop_reason.value if artifact.stop_reason else '-'}")
    print(
        f"{BOLD}reward{RESET}      "
        f"{'null (excluded from pass@k)' if reward is None else reward}"
    )
    print(f"{BOLD}verdict{RESET}     {evaluation.verdict}")
    if evaluation.near_miss:
        print(f"{YELLOW}            near miss -- structurally right, just outside tolerance{RESET}")
    print(f"{BOLD}steps{RESET}       {artifact.step_count}/{limits.max_steps}")
    if artifact.duration_s:
        print(f"{BOLD}duration{RESET}    {artifact.duration_s:.1f}s")
    print(
        f"{BOLD}tokens{RESET}      "
        f"{artifact.token_usage.total_tokens:,} "
        f"(prompt {artifact.token_usage.prompt_tokens:,}, "
        f"output {artifact.token_usage.output_tokens:,})"
    )
    print(f"{BOLD}sql editor{RESET}  {artifact.used_sql_editor}")
    if artifact.safety_acknowledgements:
        print(
            f"{BOLD}safety{RESET}      "
            f"{artifact.safety_acknowledgements} confirmation(s) auto-acknowledged"
        )
    if artifact.refusals:
        print(f"{BOLD}refusals{RESET}    {len(artifact.refusals)}")
        for refusal in artifact.refusals[:3]:
            print(f"            {DIM}{refusal}{RESET}")

    print()
    print(f"{BOLD}expected{RESET}    {evaluation.expected_output}")
    print(f"{BOLD}actual{RESET}      {evaluation.actual_output}")
    if artifact.actual_output is None and artifact.actual_output_raw:
        print(f"{BOLD}raw{RESET}         {artifact.actual_output_raw[:300]!r}")
    if evaluation.diff:
        print(f"{BOLD}diff{RESET}        {evaluation.diff}")
    if artifact.error:
        print(f"{BOLD}error{RESET}       {RED}{artifact.error}{RESET}")
    if artifact.final_text and artifact.stop_reason and artifact.stop_reason.value == "NO_FUNCTION_CALL":
        print(f"{BOLD}final text{RESET}  {artifact.final_text[:300]}")

    out_dir = executor.attempt_dir(artifact.attempt_id, artifact.job_id)
    print()
    print(f"{BOLD}artifacts{RESET}   {out_dir}")
    print(f"            {DIM}artifact.json, evaluation.json, steps/*.png, worker.log{RESET}")
    if artifact.video_path:
        print(f"{BOLD}video{RESET}       {out_dir / artifact.video_path}")

    # Exit non-zero only for infrastructure failure. A wrong answer is a valid outcome,
    # so it must not look like a broken pipeline to a caller or CI step.
    return 1 if artifact.status is AttemptStatus.ERRORED else 0


if __name__ == "__main__":
    sys.exit(main())
