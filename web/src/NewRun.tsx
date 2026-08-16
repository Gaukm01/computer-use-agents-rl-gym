import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { JobResult, Task } from "./types";

/** Left: configure and launch a run. Right: previous runs. */
export function NewRun({ onOpenJob }: { onOpenJob: (jobId: string) => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksFile, setTasksFile] = useState<string | null>(null);
  const [sourceName, setSourceName] = useState("resources/tasks.json (bundled)");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [attempts, setAttempts] = useState(1);
  const [concurrency, setConcurrency] = useState(3);
  const [maxSteps, setMaxSteps] = useState<number | "">("");
  const [recordVideo, setRecordVideo] = useState(true);

  const [jobs, setJobs] = useState<JobResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  function loadTasks(file: string | null) {
    api
      .listTasks(file)
      .then((next) => {
        setTasks(next);
        setSelected(new Set(next.map((t) => t.id)));
        setError(null);
      })
      .catch((e) => setError(String(e.message)));
  }

  useEffect(() => loadTasks(null), []);
  useEffect(() => {
    api.listJobs().then(setJobs).catch(() => undefined);
  }, []);

  async function onUpload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError(null);
    try {
      const result = await api.uploadTasks(file);
      // The stored path is what the job spec records, so the uploaded file genuinely
      // drives the run rather than only changing what's on screen.
      setTasksFile(result.tasks_file);
      setSourceName(result.filename);
      setTasks(result.tasks);
      setSelected(new Set(result.tasks.map((t) => t.id)));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function useBundled() {
    setTasksFile(null);
    setSourceName("resources/tasks.json (bundled)");
    loadTasks(null);
  }

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const job = await api.submitJob({
        task_ids: Array.from(selected),
        attempts_per_task: attempts,
        concurrency,
        tasks_file: tasksFile,
        max_steps: maxSteps === "" ? null : Number(maxSteps),
        record_video: recordVideo,
      });
      onOpenJob(job.spec.job_id);
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }

  const totalAttempts = selected.size * attempts;

  return (
    <div className="grid-2">
      <div className="panel">
        <div className="panel-head">New eval run</div>
        <div className="panel-body">
          {error && <div className="error-box">{error}</div>}

          <div className="field">
            <label>Task file</label>
            <div className="file-drop">
              <input
                ref={fileInput}
                id="tasks-upload"
                type="file"
                accept=".json,application/json"
                onChange={onUpload}
              />
              <label htmlFor="tasks-upload">Upload a tasks.json</label>
              <div style={{ marginTop: 4, fontSize: 12 }}>{sourceName}</div>
            </div>
            {tasksFile && (
              <button
                className="btn btn-sm"
                style={{ marginTop: 8 }}
                onClick={useBundled}
              >
                Use bundled tasks.json
              </button>
            )}
            <div className="hint">
              Each task needs <span className="mono">id</span>,{" "}
              <span className="mono">task</span> and <span className="mono">answer</span>{" "}
              (a JSON string). Validated on upload.
            </div>
          </div>

          <div className="field">
            <label>
              Tasks — {selected.size} of {tasks.length} selected
            </label>
            <div className="checklist">
              {tasks.map((task) => (
                <label key={task.id}>
                  <input
                    type="checkbox"
                    checked={selected.has(task.id)}
                    onChange={() => toggle(task.id)}
                  />
                  <span>
                    <span className="t-id">{task.id}</span>
                    <span className="t-text">{task.task.slice(0, 90)}…</span>
                  </span>
                </label>
              ))}
              {tasks.length === 0 && <div className="empty">no tasks loaded</div>}
            </div>
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button
                className="btn btn-sm"
                onClick={() => setSelected(new Set(tasks.map((t) => t.id)))}
              >
                Select all
              </button>
              <button className="btn btn-sm" onClick={() => setSelected(new Set())}>
                Clear
              </button>
            </div>
          </div>

          <div className="field">
            <label>Attempts per task (K)</label>
            <input
              type="number"
              min={1}
              max={20}
              value={attempts}
              onChange={(e) => setAttempts(Math.max(1, Number(e.target.value)))}
            />
            <div className="hint">pass@K is computed over attempts that produced an observable outcome.</div>
          </div>

          <div className="field">
            <label>Concurrency</label>
            <input
              type="number"
              min={1}
              max={3}
              value={concurrency}
              onChange={(e) =>
                setConcurrency(Math.min(3, Math.max(1, Number(e.target.value))))
              }
            />
            <div className="hint">Each attempt gets its own gym, so this is bounded by memory (max 3).</div>
          </div>

          <div className="field">
            <label>Max steps per attempt (optional)</label>
            <input
              type="number"
              min={1}
              max={200}
              placeholder="40"
              value={maxSteps}
              onChange={(e) =>
                setMaxSteps(e.target.value === "" ? "" : Number(e.target.value))
              }
            />
            <div className="hint">Set to 1–2 for a cheap plumbing check.</div>
          </div>

          <div className="field">
            <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={recordVideo}
                onChange={(e) => setRecordVideo(e.target.checked)}
                style={{ width: "auto" }}
              />
              Record video
            </label>
          </div>

          <button
            className="btn btn-primary"
            style={{ width: "100%" }}
            onClick={run}
            disabled={busy || selected.size === 0}
          >
            {busy ? "Starting…" : `Run ${totalAttempts} attempt(s)`}
          </button>

          {totalAttempts > 6 && maxSteps === "" && (
            <div className="hint" style={{ marginTop: 8, color: "var(--warn-fg)" }}>
              Heads up: a full attempt costs roughly 500k tokens and 4–5 minutes, so this
              run is around {(totalAttempts * 0.5).toFixed(1)}M tokens.
            </div>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">Previous runs</div>
        {jobs.length === 0 ? (
          <div className="empty">no runs yet</div>
        ) : (
          jobs.map((job) => (
            <div
              className="job-row"
              key={job.spec.job_id}
              onClick={() => onOpenJob(job.spec.job_id)}
            >
              <span className="job-id">{job.spec.job_id}</span>
              <span
                className={`pill ${
                  job.status === "COMPLETED"
                    ? "pill-pass"
                    : job.status === "RUNNING"
                      ? "pill-info"
                      : "pill-muted"
                }`}
              >
                {job.status}
              </span>
              <span className="spacer" />
              <span className="muted">
                {job.spec.task_ids.length}×{job.spec.attempts_per_task}
              </span>
              <span className="pill pill-pass">{job.passed} passed</span>
              <span className="pill pill-fail">{job.failed} failed</span>
              {job.errored > 0 && (
                <span className="pill pill-warn">{job.errored} errored</span>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
