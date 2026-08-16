"""Run a full job from the command line: T tasks x K attempts, in parallel.

Exercises exactly the service layer the API will call, so anything verified here is
verified for the platform.

    # every task, 1 attempt each (the cheap default)
    agent/.venv/bin/python -m rlgym.scripts.run_job

    # pass@3 over three tasks
    agent/.venv/bin/python -m rlgym.scripts.run_job \
        --tasks problem10,problem5,problem6 --attempts 3 --concurrency 3

    # continue an interrupted job -- finished attempts are not re-run
    agent/.venv/bin/python -m rlgym.scripts.run_job --resume job-20260815-...
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from .. import orchestrator, service, vendor
from ..schemas import JobResult, JobStatus

load_dotenv(vendor.PROJECT_ROOT / ".env")

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)


def render(job: JobResult) -> None:
    spec = job.spec
    print()
    print(f"{BOLD}job{RESET}          {spec.job_id}")
    print(f"{BOLD}status{RESET}       {job.status.value}")
    print(
        f"{BOLD}scope{RESET}        {len(spec.task_ids)} task(s) x "
        f"{spec.attempts_per_task} attempt(s) = {spec.total_attempts}, "
        f"concurrency {spec.concurrency} (each attempt gets its own gym)"
    )
    if job.duration_s:
        print(f"{BOLD}duration{RESET}     {job.duration_s:.0f}s")
    print(f"{BOLD}tokens{RESET}       {job.total_tokens:,}")
    print()

    header = f"{'task':<12} {'pass@k':<8} {'passed':<7} {'failed':<7} {'errored':<8} {'rate':<7}"
    print(BOLD + header + RESET)
    print(DIM + "-" * len(header) + RESET)
    for task in job.tasks:
        rate = f"{task.pass_rate:.0%}" if task.pass_rate is not None else "-"
        mark = f"{GREEN}yes{RESET}" if task.pass_at_k else f"{RED}no{RESET} "
        flag = f"  {YELLOW}low-confidence ({task.valid_attempts}/{spec.attempts_per_task} valid){RESET}" if task.low_confidence else ""
        print(
            f"{task.task_id:<12} {mark:<17} {task.passed:<7} {task.failed:<7} "
            f"{task.errored:<8} {rate:<7}{flag}"
        )

    print()
    if job.pass_at_k_rate is not None:
        print(
            f"{BOLD}pass@{spec.attempts_per_task}{RESET}       "
            f"{job.pass_at_k_rate:.0%} of graded tasks had at least one passing attempt"
        )
    if job.mean_reward is not None:
        print(
            f"{BOLD}mean reward{RESET}  {job.mean_reward:.2f} "
            f"over {job.valid_attempts} valid attempt(s)"
        )
    if job.errored:
        print(
            f"{YELLOW}{job.errored} attempt(s) NOT OBSERVED -- excluded from pass@k, "
            f"not counted as failures{RESET}"
        )
    print()
    print(f"{BOLD}artifacts{RESET}    {orchestrator.job_dir(spec.job_id)}")
    print(f"             {DIM}job.json + one directory per attempt{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a job against the gym.")
    parser.add_argument(
        "--tasks",
        default=None,
        help="Comma-separated task ids. Default: every task in the file.",
    )
    parser.add_argument("--tasks-file", default=None)
    parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="Attempts per task (K). Default 1; pass 3 for pass@3.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Parallel attempts, each with its own gym. Capped at ports.MAX_GYMS (memory-bound).",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--hint", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--resume", default=None, metavar="JOB_ID")
    args = parser.parse_args()

    try:
        if args.resume:
            print(f"resuming {args.resume} ...")
            job = service.resume_job(args.resume, block=True)
        else:
            task_ids = (
                [t.strip() for t in args.tasks.split(",") if t.strip()]
                if args.tasks
                else None
            )
            job = service.submit_job(
                task_ids,
                attempts_per_task=args.attempts,
                concurrency=args.concurrency,
                model=args.model,
                hint=args.hint,
                max_steps=args.max_steps,
                timeout_s=args.timeout,
                headless=not args.headed,
                record_video=not args.no_video,
                tasks_file=args.tasks_file,
                block=True,  # CLI runs inline so the process doesn't exit early
            )
    except (KeyError, ValueError) as exc:
        print(f"{RED}{exc}{RESET}")
        return 2

    render(job)
    # Non-zero only when the orchestrator itself failed. Tasks failing is a valid result.
    return 1 if job.status is JobStatus.FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
