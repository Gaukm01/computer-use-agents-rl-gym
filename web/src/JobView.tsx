import { useEffect, useState } from "react";
import { api } from "./api";
import { Transcript } from "./Transcript";
import type {
  AttemptPhase,
  AttemptSummary,
  JobResult,
  RunStats,
  TaskResult,
} from "./types";

const POLL_MS = 2000;

/** Setup work an attempt goes through before the agent does anything. Provisioning alone
 *  takes ~35s, so without this a new attempt just looks hung. */
const PHASE_STEPS: { phase: AttemptPhase; label: string }[] = [
  { phase: "PROVISIONING", label: "Booting gym — Postgres + Metabase" },
  { phase: "AUTHENTICATING", label: "Minting a session" },
  { phase: "CONNECTING", label: "Attaching the browser" },
  { phase: "RUNNING", label: "Agent working" },
  { phase: "GRADING", label: "Grading" },
];

const PHASE_ORDER: AttemptPhase[] = [
  "QUEUED",
  "PROVISIONING",
  "AUTHENTICATING",
  "CONNECTING",
  "RUNNING",
  "GRADING",
  "DONE",
];

const TERMINAL_STATUSES = new Set([
  "COMPLETED_CORRECT",
  "ANSWERED",
  "MAX_STEPS_EXCEEDED",
  "TIMED_OUT",
  "NOT_OBSERVED",
]);

const isTerminal = (a: AttemptSummary) => TERMINAL_STATUSES.has(a.status);
const hasStarted = (a: AttemptSummary) => a.attempt_id !== "";

function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 90) return `${value.toFixed(0)}s`;
  return `${Math.floor(value / 60)}m ${(value % 60).toFixed(0)}s`;
}

function chipClass(attempt: AttemptSummary): string {
  if (!hasStarted(attempt)) return "chip chip-idle";
  if (attempt.status === "COMPLETED_CORRECT") return "chip chip-pass";
  if (attempt.status === "NOT_OBSERVED") return "chip chip-warn";
  if (!isTerminal(attempt)) return "chip chip-running";
  return "chip chip-fail";
}

function chipLabel(attempt: AttemptSummary, jobEnded: boolean): string {
  if (!hasStarted(attempt)) return jobEnded ? "Not run" : "Queued";
  switch (attempt.status) {
    case "COMPLETED_CORRECT":
      return "Passed";
    case "NOT_OBSERVED":
      // Deliberately not "Failed": its outcome was never seen, so it is not a loss.
      return "Not observed";
    case "MAX_STEPS_EXCEEDED":
      return "Out of steps";
    case "TIMED_OUT":
      return "Timed out";
    case "ANSWERED":
      return "Failed";
    default:
      return "Running";
  }
}

/** Reward, exactly as the RL signal defines it: 1, 0, or null for an unobserved outcome. */
function rewardText(attempt: AttemptSummary): string | null {
  if (!isTerminal(attempt)) return null;
  return attempt.reward === null ? "reward null" : `reward ${attempt.reward}`;
}

function PhaseChecklist({ attempt }: { attempt: AttemptSummary }) {
  const current = PHASE_ORDER.indexOf(attempt.phase);
  return (
    <div className="phase-list">
      <div className="phase-head">
        attempt {attempt.attempt_index + 1} — in progress
      </div>
      {PHASE_STEPS.map(({ phase, label }) => {
        const at = PHASE_ORDER.indexOf(phase);
        const state = at < current ? "done" : at === current ? "now" : "todo";
        return (
          <div className={`phase-step phase-${state}`} key={phase}>
            <span className="phase-mark">
              {state === "done" ? "✓" : state === "now" ? "●" : "○"}
            </span>
            <span>{label}</span>
            {phase === "RUNNING" && state !== "todo" && attempt.step_count > 0 && (
              <span className="muted"> · step {attempt.step_count}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** passed / failed / not observed / running / queued, in one bar. */
function ProgressBar({
  passed,
  failed,
  errored,
  running,
  queued,
}: {
  passed: number;
  failed: number;
  errored: number;
  running: number;
  queued: number;
}) {
  const total = passed + failed + errored + running + queued || 1;
  const segments = [
    { key: "passed", n: passed, cls: "seg-pass" },
    { key: "failed", n: failed, cls: "seg-fail" },
    { key: "errored", n: errored, cls: "seg-warn" },
    { key: "running", n: running, cls: "seg-run" },
    { key: "queued", n: queued, cls: "seg-idle" },
  ];
  return (
    <div className="progress-bar">
      {segments
        .filter((s) => s.n > 0)
        .map((s) => (
          <div
            key={s.key}
            className={`progress-seg ${s.cls}`}
            style={{ width: `${(s.n / total) * 100}%` }}
            title={`${s.n} ${s.key}`}
          />
        ))}
    </div>
  );
}

function StatsBox({ stats, label }: { stats: RunStats; label: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="stats-box">
      <button className="stats-toggle" onClick={() => setOpen((o) => !o)}>
        <span className="mono muted">{open ? "▾" : "▸"}</span> {label}
      </button>
      {open && (
        <div className="stats-grid">
          <div>
            <div className="stat-label">tokens used</div>
            <div className="stat-value">{stats.total_tokens.toLocaleString()}</div>
          </div>
          <div>
            <div className="stat-label">total agent time</div>
            <div className="stat-value">{seconds(stats.total_duration_s)}</div>
          </div>
          <div>
            <div className="stat-label">avg time / rollout</div>
            <div className="stat-value">{seconds(stats.avg_duration_s)}</div>
          </div>
          <div>
            <div className="stat-label">avg steps / rollout</div>
            <div className="stat-value">
              {stats.avg_steps === null ? "—" : stats.avg_steps.toFixed(1)}
            </div>
          </div>
          <div>
            <div className="stat-label">rollouts measured</div>
            <div className="stat-value">{stats.observed_attempts}</div>
          </div>
        </div>
      )}
      {open && (
        <div className="hint" style={{ padding: "0 12px 10px" }}>
          Averages cover rollouts the agent actually ran.
        </div>
      )}
    </div>
  );
}

function TaskCard({
  task,
  jobId,
  jobEnded,
}: {
  task: TaskResult;
  jobId: string;
  jobEnded: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const running = task.attempts.filter((a) => hasStarted(a) && !isTerminal(a));

  return (
    <div className="task-card">
      <div className="task-card-head" onClick={() => setOpen((o) => !o)}>
        <span className="muted mono">{open ? "▾" : "▸"}</span>
        <span className="task-id">{task.task_id}</span>
        <span className="task-prompt">{task.prompt}</span>
        {/* Visible without expanding: how expensive this task is per rollout is often
            the reason to look at it at all. */}
        {task.stats.avg_duration_s !== null && (
          <span className="muted mono" title="average per rollout the agent actually ran">
            {seconds(task.stats.avg_duration_s)} · {task.stats.avg_steps?.toFixed(0)}{" "}
            steps
          </span>
        )}
        <span className="muted mono">
          {task.completed}/{task.total_attempts}
        </span>
        {task.passed > 0 && <span className="pill pill-pass">{task.passed} ✓</span>}
        {task.failed > 0 && <span className="pill pill-fail">{task.failed} ✗</span>}
        {task.errored > 0 && (
          <span className="pill pill-warn">{task.errored} not observed</span>
        )}
      </div>

      {open && (
        <div className="task-card-body">
          <div className="prompt-box">{task.prompt}</div>

          <div style={{ marginBottom: 12 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              Expected answer:{" "}
            </span>
            <span className="mono">{JSON.stringify(task.expected_output)}</span>
          </div>

          <div className="chip-row" style={{ marginBottom: 10 }}>
            {task.attempts.map((attempt) => {
              const openable = hasStarted(attempt) && isTerminal(attempt);
              return (
                <button
                  key={`${task.task_id}-${attempt.attempt_index}`}
                  className={`${chipClass(attempt)}${
                    selected === attempt.attempt_id ? " selected" : ""
                  }`}
                  disabled={!openable}
                  title={
                    openable
                      ? "Open the transcript"
                      : "The transcript opens once this rollout finishes"
                  }
                  onClick={() =>
                    setSelected((current) =>
                      current === attempt.attempt_id ? null : attempt.attempt_id,
                    )
                  }
                >
                  <span>{attempt.attempt_index + 1}</span>
                  <span>{chipLabel(attempt, jobEnded)}</span>
                  {rewardText(attempt) && (
                    <span className="chip-reward">{rewardText(attempt)}</span>
                  )}
                </button>
              );
            })}
          </div>

          {running.map((attempt) => (
            <PhaseChecklist key={attempt.attempt_id} attempt={attempt} />
          ))}

          <StatsBox stats={task.stats} label="Rollout statistics for this task" />

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

  const live = job.status === "RUNNING" || job.status === "QUEUED";
  const jobEnded = !live;
  const statusPill =
    job.status === "COMPLETED"
      ? "pill-pass"
      : live
        ? "pill-info"
        : job.status === "FAILED"
          ? "pill-fail"
          : "pill-muted";

  const agent = job.spec.agent_config;
  const scored = job.passed + job.failed;

  return (
    <div>
      <div className="btn-row" style={{ marginBottom: 16 }}>
        <button className="btn btn-sm" onClick={onBack}>
          ← All runs
        </button>
        <span className="job-id">{job.spec.job_id}</span>
        <span className={`pill ${statusPill}`}>{job.status}</span>
        {live && (
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
          {/* The headline is withheld until the job is over. A rate computed off three
              finished rollouts out of thirty is not the run's result, and showing it as
              one invites reading a number that is about to change. */}
          {jobEnded && scored > 0 && (
            <div className="headline">
              <span className="headline-value">
                {Math.round((job.passed / scored) * 100)}%
              </span>
              <span className="headline-label">
                passed — {job.passed} of {scored} scored rollout(s)
                {job.errored > 0 && `, ${job.errored} not observed`}
              </span>
            </div>
          )}

          <ProgressBar
            passed={job.passed}
            failed={job.failed}
            errored={job.errored}
            running={job.running_attempts}
            queued={job.queued_attempts}
          />

          <div className="counts">
            <span className="count count-done">
              <b>{job.completed_attempts}</b> processed
            </span>
            <span className="count count-run">
              <b>{job.running_attempts}</b> running
            </span>
            <span className="count count-idle">
              <b>{job.queued_attempts}</b> in queue
            </span>
            <span className="spacer" />
            <span className="count count-pass">
              <b>{job.passed}</b> passed
            </span>
            <span className="count count-fail">
              <b>{job.failed}</b> failed
            </span>
            {job.errored > 0 && (
              <span className="count count-warn">
                <b>{job.errored}</b> not observed
              </span>
            )}
          </div>

          {/* Which policy produced these numbers. Locked to the run: it is read from the
              job's own spec, so it reports what actually ran, not the form's current state. */}
          <div className="agent-line">
            <span className="mono">{agent.model}</span>
            <span className="muted mono">
              temp {agent.temperature} · top_p {agent.top_p} · top_k {agent.top_k}
            </span>
            <span className="muted mono">
              {job.spec.limits.max_steps} steps / {job.spec.limits.timeout_s}s per rollout
            </span>
            <span className="muted mono">
              step-budget awareness {agent.budget_reminders ? "on" : "off"}
            </span>
            {agent.hint && <span className="pill pill-warn">hint given</span>}
          </div>

          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            {job.spec.task_ids.length} task(s) × {job.spec.attempts_per_task} rollout(s) ·
            concurrency {job.spec.concurrency} (own gym each)
            {job.duration_s !== null && ` · ran ${seconds(job.duration_s)}`}
            {job.spec.tasks_file
              ? ` · ${job.spec.tasks_file.split("/").pop()}`
              : ""}
          </div>
        </div>
      </div>

      <StatsBox stats={job.stats} label="Run statistics" />

      <div style={{ marginTop: 16 }}>
        {job.tasks.map((task) => (
          <TaskCard
            key={task.task_id}
            task={task}
            jobId={jobId}
            jobEnded={jobEnded}
          />
        ))}
      </div>
    </div>
  );
}
