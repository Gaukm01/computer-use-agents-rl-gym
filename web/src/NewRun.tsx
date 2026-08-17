import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { AgentDefaults, JobResult, Task } from "./types";

/** A number field you can actually type in.
 *
 *  The obvious `value={n} onChange={setN(Math.max(1, Number(v)))}` is unusable: clearing
 *  the box yields "", `Number("")` is 0, the clamp snaps it back to 1, and the field can
 *  never be edited — only stepped. So the raw text is what's held in state, and it is
 *  coerced once, at submit.
 */
function NumberField({
  value,
  onChange,
  ...rest
}: {
  value: string;
  onChange: (next: string) => void;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  return (
    <input
      type="text"
      inputMode="numeric"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      {...rest}
    />
  );
}

/** Click-to-open explainer. A `title` tooltip is too easy to miss for something that
 *  determines how long a run takes and how much it costs. */
function Info({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="info-wrap">
      <button
        type="button"
        className="info-dot"
        aria-label="more information"
        onClick={() => setOpen((o) => !o)}
      >
        i
      </button>
      {open && <span className="info-pop">{children}</span>}
    </span>
  );
}

function parsePositive(text: string, fallback: number): number {
  const value = Number(text.trim());
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallback;
}

function parseNumber(text: string, fallback: number): number {
  const value = Number(text.trim());
  return Number.isFinite(value) ? value : fallback;
}

/** Left: configure and launch a run. Right: previous runs. */
export function NewRun({ onOpenJob }: { onOpenJob: (jobId: string) => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksFile, setTasksFile] = useState<string | null>(null);
  const [sourceName, setSourceName] = useState("resources/tasks.json (bundled)");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [attempts, setAttempts] = useState("1");
  const [concurrency, setConcurrency] = useState("1");
  const [maxSteps, setMaxSteps] = useState("");

  // Agent controls. Defaults come from the server so the form can't drift from the
  // harness — whatever the harness treats as default is what's shown here.
  const [defaults, setDefaults] = useState<AgentDefaults | null>(null);
  const [model, setModel] = useState("");
  const [temperature, setTemperature] = useState("");
  const [topP, setTopP] = useState("");
  const [topK, setTopK] = useState("");
  const [budgetReminders, setBudgetReminders] = useState(true);
  const [showAgentControls, setShowAgentControls] = useState(false);

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
    api
      .agentDefaults()
      .then((d) => {
        setDefaults(d);
        setModel(d.default.model);
        setTemperature(String(d.default.temperature));
        setTopP(String(d.default.top_p));
        setTopK(String(d.default.top_k));
        setBudgetReminders(d.default.budget_reminders);
      })
      .catch(() => undefined);
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

  const k = parsePositive(attempts, 1);
  const lanes = Math.min(
    parsePositive(concurrency, 1),
    defaults?.max_gyms ?? 3,
  );
  const totalAttempts = selected.size * k;
  const capSeconds = defaults?.timeout_s ?? 600;
  // Each attempt boots its own Postgres + Metabase before the agent starts; measured at
  // ~35s. The cap applies to agent time only, so the worst case per attempt is cap + boot.
  const worstCaseMinutes = Math.ceil(
    (Math.ceil(totalAttempts / Math.max(lanes, 1)) * (capSeconds + 35)) / 60,
  );

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const job = await api.submitJob({
        task_ids: Array.from(selected),
        attempts_per_task: k,
        concurrency: lanes,
        tasks_file: tasksFile,
        max_steps: maxSteps.trim() === "" ? null : parsePositive(maxSteps, 40),
        record_video: recordVideo,
        agent: {
          model: model || undefined,
          temperature: parseNumber(
            temperature,
            defaults?.default.temperature ?? 1,
          ),
          top_p: parseNumber(topP, defaults?.default.top_p ?? 0.95),
          top_k: parsePositive(topK, defaults?.default.top_k ?? 40),
          budget_reminders: budgetReminders,
        },
      });
      onOpenJob(job.spec.job_id);
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }

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
              <span className="mono">task</span> and{" "}
              <span className="mono">answer</span>. The answer may be JSON or a plain
              value.
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

          <div className="field-row">
            <div className="field">
              <label>
                Attempts per task
                <Info>
                  Independent rollouts, each in a fresh gym, capped at {capSeconds}s.
                  <br />
                  {totalAttempts} at {lanes} in parallel: at most{" "}
                  <b>~{worstCaseMinutes} min</b>.
                </Info>
              </label>
              <NumberField value={attempts} onChange={setAttempts} />
            </div>

            <div className="field">
              <label>
                Concurrency
                <Info>
                  Rollouts at once, each with its own gym. Memory-bound, max{" "}
                  {defaults?.max_gyms ?? 3}.
                </Info>
              </label>
              <NumberField value={concurrency} onChange={setConcurrency} />
            </div>
          </div>

          <div className="field">
            <label>Model</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {(defaults?.models ?? []).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>

          <div className="control-row">
            <button
              className="btn btn-sm"
              onClick={() => setShowAgentControls((s) => !s)}
            >
              {showAgentControls ? "▾" : "▸"} More agent controls
            </button>
            <label className="inline-check">
              <input
                type="checkbox"
                checked={recordVideo}
                onChange={(e) => setRecordVideo(e.target.checked)}
              />
              Record video
            </label>
          </div>

          {showAgentControls && (
            <div className="sub-panel">
              <div className="field-row">
                <div className="field">
                  <label>Temperature</label>
                  <NumberField value={temperature} onChange={setTemperature} />
                </div>
                <div className="field">
                  <label>Top P</label>
                  <NumberField value={topP} onChange={setTopP} />
                </div>
                <div className="field">
                  <label>Top K</label>
                  <NumberField value={topK} onChange={setTopK} />
                </div>
              </div>
              <div className="field">
                <label>Max steps per attempt</label>
                <NumberField
                  value={maxSteps}
                  onChange={setMaxSteps}
                  placeholder={String(defaults?.max_steps ?? 40)}
                />
              </div>
              <label className="inline-check">
                <input
                  type="checkbox"
                  checked={budgetReminders}
                  onChange={(e) => setBudgetReminders(e.target.checked)}
                />
                Step-budget awareness
                <Info>
                  Tells the agent how many steps remain, and warns it as the cap nears.
                  <br />
                  Off measures whether it can manage a budget it cannot see.
                </Info>
              </label>
            </div>
          )}

          <button
            className="btn btn-primary"
            style={{ width: "100%" }}
            onClick={run}
            disabled={busy || selected.size === 0}
          >
            {busy ? "Starting…" : `Run ${totalAttempts} rollout(s)`}
          </button>
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
              <span className="muted mono">{job.spec.agent_config?.model}</span>
              <span className="muted">
                {job.spec.task_ids.length}×{job.spec.attempts_per_task}
              </span>
              <span className="pill pill-pass">{job.passed} passed</span>
              <span className="pill pill-fail">{job.failed} failed</span>
              {job.errored > 0 && (
                <span className="pill pill-warn">{job.errored} not observed</span>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
