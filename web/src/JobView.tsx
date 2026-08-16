import { useEffect, useState } from "react";
import { api } from "./api";
import { Transcript } from "./Transcript";
import type { AttemptSummary, JobResult, TaskResult } from "./types";

const POLL_MS = 3000;

function attemptChipClass(attempt: AttemptSummary): string {
  if (attempt.status === "COMPLETED_CORRECT") return "chip chip-pass";
  if (attempt.status === "NOT_OBSERVED") return "chip chip-warn";
  if (attempt.status === "IN_PROGRESS" || attempt.status === "QUEUED")
    return "chip chip-running";
  return "chip chip-fail";
}

/** Phase is what makes the ~2 minutes of provisioning legible instead of looking hung. */
function phaseLabel(attempt: AttemptSummary): string | null {
  if (attempt.status === "IN_PROGRESS" || attempt.status === "QUEUED") {
    return attempt.phase.toLowerCase();
  }
  if (attempt.status === "NOT_OBSERVED" && attempt.phase !== "DONE") {
    return `failed in ${attempt.phase.toLowerCase()}`;
  }
  return null;
}

function attemptChipLabel(attempt: AttemptSummary): string {
  switch (attempt.status) {
    case "COMPLETED_CORRECT":
      return "Passed";
    case "NOT_OBSERVED":
      // Deliberately not "Failed": reward is null and it's out of the pass@k denominator.
      return "Not observed";
    case "MAX_STEPS_EXCEEDED":
      return "Max steps";
    case "TIMED_OUT":
      return "Timed out";
    case "IN_PROGRESS":
      return "Running";
    case "QUEUED":
      return "Queued";
    default:
      return "Failed";
  }
}

function TaskCard({
  task,
  jobId,
  requestedAttempts,
}: {
  task: TaskResult;
  jobId: string;
  requestedAttempts: number;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const rate =
    task.pass_rate === null ? "—" : `${Math.round(task.pass_rate * 100)}% pass rate`;

  return (
    <div className="task-card">
      <div className="task-card-head" onClick={() => setOpen((o) => !o)}>
        <span className="muted mono">{open ? "▾" : "▸"}</span>
        <span className="task-id">{task.task_id}</span>
        <span className="task-prompt">{task.prompt}</span>
        {task.low_confidence && (
          <span
            className="pill pill-warn"
            title={`only ${task.valid_attempts} of ${requestedAttempts} attempts produced an observable outcome`}
          >
            low confidence
          </span>
        )}
        <span className={`pill ${task.pass_at_k ? "pill-pass" : "pill-fail"}`}>
          {task.pass_at_k ? "pass@k" : "no pass"}
        </span>
        <span className="muted mono">{rate}</span>
      </div>

      {open && (
        <div className="task-card-body">
          <div className="prompt-box">{task.prompt}</div>

          <div style={{ marginBottom: 10 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              Expected answer:{" "}
            </span>
            <span className="mono">{JSON.stringify(task.expected_output)}</span>
          </div>

          <div className="chip-row" style={{ marginBottom: 4 }}>
            {task.attempts.map((attempt) => (
              <button
                key={attempt.attempt_id}
                className={`${attemptChipClass(attempt)}${
                  selected === attempt.attempt_id ? " selected" : ""
                }`}
                onClick={() =>
                  setSelected((current) =>
                    current === attempt.attempt_id ? null : attempt.attempt_id,
                  )
                }
              >
                <span>{attempt.attempt_index + 1}</span>
                <span>
                  {attempt.status === "COMPLETED_CORRECT" ? "✓" : "✗"}{" "}
                  {attemptChipLabel(attempt)}
                </span>
                {phaseLabel(attempt) && (
                  <span style={{ opacity: 0.7 }}>· {phaseLabel(attempt)}</span>
                )}
              </button>
            ))}
            {task.attempts.length === 0 && (
              <span className="muted" style={{ fontSize: 12 }}>
                no attempts finished yet
              </span>
            )}
          </div>

          {selected && (
            <div style={{ marginTop: 16 }}>
              <Transcript jobId={jobId} attemptId={selected} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function JobView({ jobId, onBack }: { jobId: string; onBack: () => void }) {
  const [job, setJob] = useState<JobResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;

    async function poll() {
      try {
        const next = await api.getJob(jobId);
        if (!active) return;
        setJob(next);
        // Polling job.json is enough to follow a live run -- no websockets needed.
        if (next.status === "RUNNING" || next.status === "QUEUED") {
          timer = window.setTimeout(poll, POLL_MS);
        }
      } catch (e) {
        if (active) setError(String((e as Error).message));
      }
    }
    poll();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId]);

  if (error) return <div className="error-box">{error}</div>;
  if (!job) return <div className="empty">loading job…</div>;

  const running = job.status === "RUNNING" || job.status === "QUEUED";
  const statusPill =
    job.status === "COMPLETED"
      ? "pill-pass"
      : running
        ? "pill-info"
        : job.status === "FAILED"
          ? "pill-fail"
          : "pill-muted";

  return (
    <div>
      <div className="btn-row" style={{ marginBottom: 16 }}>
        <button className="btn btn-sm" onClick={onBack}>
          ← All runs
        </button>
        <span className="job-id">{job.spec.job_id}</span>
        <span className={`pill ${statusPill}`}>{job.status}</span>
        {running && (
          <button
            className="btn btn-sm"
            onClick={() => api.cancelJob(jobId).catch(() => undefined)}
          >
            Cancel
          </button>
        )}
      </div>

      <div className="panel" style={{ marginBottom: 20 }}>
        <div className="panel-body">
          <div className="stat-row">
            <div>
              <div className="stat-label">progress</div>
              <div className="stat-value">
                {job.completed_attempts}/{job.total_attempts}
              </div>
            </div>
            <div>
              <div className="stat-label">pass@{job.spec.attempts_per_task}</div>
              <div className="stat-value">
                {job.pass_at_k_rate === null
                  ? "—"
                  : `${Math.round(job.pass_at_k_rate * 100)}%`}
              </div>
            </div>
            <div>
              <div className="stat-label">passed</div>
              <div className="stat-value" style={{ color: "var(--pass-fg)" }}>
                {job.passed}
              </div>
            </div>
            <div>
              <div className="stat-label">failed</div>
              <div className="stat-value" style={{ color: "var(--fail-fg)" }}>
                {job.failed}
              </div>
            </div>
            <div>
              <div className="stat-label">errored</div>
              <div className="stat-value" style={{ color: "var(--warn-fg)" }}>
                {job.errored}
              </div>
            </div>
            <div>
              <div className="stat-label">tokens</div>
              <div className="stat-value">{job.total_tokens.toLocaleString()}</div>
            </div>
            {job.duration_s !== null && (
              <div>
                <div className="stat-label">duration</div>
                <div className="stat-value">{job.duration_s.toFixed(0)}s</div>
              </div>
            )}
          </div>

          {job.errored > 0 && (
            <div style={{ marginTop: 14, fontSize: 12 }} className="muted">
              {job.errored} attempt(s) not observed — excluded from pass@k rather than
              counted as failures, since their outcome was never seen.
            </div>
          )}
          <div style={{ marginTop: 10, fontSize: 12 }} className="muted">
            {job.spec.task_ids.length} task(s) × {job.spec.attempts_per_task} attempt(s) ·
            concurrency {job.spec.concurrency} (own gym each) · {job.spec.agent_config.model} ·{" "}
            {job.spec.limits.max_steps} steps / {job.spec.limits.timeout_s}s budget
            {job.spec.tasks_file ? ` · ${job.spec.tasks_file.split("/").pop()}` : ""}
          </div>
        </div>
      </div>

      {job.tasks.map((task) => (
        <TaskCard
          key={task.task_id}
          task={task}
          jobId={jobId}
          requestedAttempts={job.spec.attempts_per_task}
        />
      ))}
    </div>
  );
}
