import { useEffect, useState } from "react";
import { api } from "./api";
import type { AttemptDetail, StepRecord } from "./types";

/** One step = one slide: what the agent saw, what it was thinking, what it did. */
function Slide({
  step,
  jobId,
  attemptId,
}: {
  step: StepRecord;
  jobId: string;
  attemptId: string;
}) {
  return (
    <div className="transcript">
      <div className="screen-pane">
        {step.screenshot ? (
          <img
            src={api.mediaUrl(jobId, attemptId, step.screenshot)}
            alt={`step ${step.index}`}
          />
        ) : (
          <span className="muted">
            no screenshot for this step (the answer submission carries none)
          </span>
        )}
      </div>

      <div className="think-pane">
        <div className="think-label">AGENT REASONING</div>
        <div className="think-text">
          {step.reasoning?.trim() || (
            <span className="muted">no reasoning text on this turn</span>
          )}
        </div>

        <div className="think-label">ACTION</div>
        <div className="action-box">
          <div className="action-name">{step.action_name}</div>
          {Object.entries(step.action_args ?? {}).map(([key, value]) => (
            <div className="action-arg" key={key}>
              {key}: <b>{typeof value === "string" ? value : JSON.stringify(value)}</b>
            </div>
          ))}
          {step.url && (
            <div className="action-arg" style={{ marginTop: 6 }}>
              url: <b>{step.url}</b>
            </div>
          )}
        </div>

        {step.refused && (
          <div className="error-box" style={{ marginTop: 12 }}>
            Refused by the environment: {step.refused_reason}
          </div>
        )}
        {step.safety_acknowledged && (
          <div style={{ marginTop: 12 }}>
            <span className="pill pill-warn">safety confirmation auto-acknowledged</span>
          </div>
        )}
      </div>
    </div>
  );
}

export function Transcript({
  jobId,
  attemptId,
}: {
  jobId: string;
  attemptId: string;
}) {
  const [detail, setDetail] = useState<AttemptDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  const [view, setView] = useState<"steps" | "video">("steps");

  useEffect(() => {
    setDetail(null);
    setError(null);
    setIndex(0);
    api.getAttempt(jobId, attemptId).then(setDetail).catch((e) => setError(String(e.message)));
  }, [jobId, attemptId]);

  // Arrow keys page through the transcript -- reviewing 40 steps by clicking is tedious.
  useEffect(() => {
    const steps = detail?.artifact.steps.length ?? 0;
    function onKey(event: KeyboardEvent) {
      if (view !== "steps" || steps === 0) return;
      if (event.key === "ArrowLeft") setIndex((i) => Math.max(0, i - 1));
      if (event.key === "ArrowRight") setIndex((i) => Math.min(steps - 1, i + 1));
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail, view]);

  if (error) return <div className="error-box">{error}</div>;
  if (!detail) return <div className="empty">loading transcript…</div>;

  const { artifact, evaluation, video } = detail;
  const steps = artifact.steps;
  const step = steps[Math.min(index, Math.max(0, steps.length - 1))];

  const gradeClass =
    evaluation?.verdict === "pass"
      ? "grade-pass"
      : evaluation?.verdict === "not_graded"
        ? "grade-warn"
        : "grade-fail";

  const message =
    evaluation?.verdict === "pass"
      ? "Test passed: answer matches the expected answer."
      : evaluation?.verdict === "not_graded"
        ? "Not graded: the outcome was never observed, so this attempt is excluded from pass@k."
        : (evaluation?.diff ?? "Test failed.");

  return (
    <div>
      <div className={`grade ${gradeClass}`}>
        <div className={`grade-head ${gradeClass}`}>GRADE</div>
        <div className="grade-body">
          <div className="grade-row">
            <span className="grade-key">Message:</span>
            <span className="grade-val">{message}</span>
          </div>
          <div className="grade-row">
            <span className="grade-key">Success:</span>
            <span className="grade-val">{String(evaluation?.verdict === "pass")}</span>
          </div>
          <div className="grade-row">
            <span className="grade-key">Reward:</span>
            <span className="grade-val">
              {artifact.status === "ERRORED"
                ? "null — excluded from pass@k, not counted as a failure"
                : String(evaluation?.reward ?? "—")}
            </span>
          </div>
          <div className="grade-row">
            <span className="grade-key">Expected:</span>
            <span className="grade-val">{JSON.stringify(evaluation?.expected_output)}</span>
          </div>
          <div className="grade-row">
            <span className="grade-key">Actual:</span>
            <span className="grade-val">
              {artifact.actual_output !== null && artifact.actual_output !== undefined
                ? JSON.stringify(artifact.actual_output)
                : artifact.actual_output_raw
                  ? `unparseable: ${artifact.actual_output_raw.slice(0, 200)}`
                  : "— (no answer submitted)"}
            </span>
          </div>
          {evaluation?.near_miss && (
            <div className="grade-row">
              <span className="grade-key">Note:</span>
              <span className="grade-val">
                near miss — structurally right, just outside tolerance
              </span>
            </div>
          )}
          {artifact.error && (
            <div className="grade-row">
              <span className="grade-key">Error:</span>
              <span className="grade-val">{artifact.error}</span>
            </div>
          )}
        </div>
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 10,
          flexWrap: "wrap",
        }}
      >
        <div className="view-toggle">
          <button
            className={view === "steps" ? "active" : ""}
            onClick={() => setView("steps")}
          >
            Steps ({steps.length})
          </button>
          <button
            className={view === "video" ? "active" : ""}
            onClick={() => setView("video")}
            disabled={!video}
            title={video ? "" : "no video recorded for this attempt"}
          >
            Video
          </button>
        </div>
        <span className="muted mono">
          {artifact.stop_reason} · {artifact.token_usage.total_tokens.toLocaleString()} tokens
          {artifact.duration_s ? ` · ${artifact.duration_s.toFixed(0)}s` : ""}
          {artifact.used_sql_editor ? " · used SQL editor" : " · query builder"}
        </span>
      </div>

      {view === "video" ? (
        <div className="transcript" style={{ gridTemplateColumns: "1fr" }}>
          <div className="screen-pane">
            {video ? (
              <video controls src={api.mediaUrl(jobId, attemptId, video)} />
            ) : (
              <span className="muted">no video for this attempt</span>
            )}
          </div>
        </div>
      ) : steps.length === 0 ? (
        <div className="empty">
          no steps recorded — the attempt failed before it could act
        </div>
      ) : (
        <>
          <Slide step={step} jobId={jobId} attemptId={attemptId} />
          <div className="slide-bar">
            <button
              className="btn btn-sm"
              onClick={() => setIndex((i) => Math.max(0, i - 1))}
              disabled={index === 0}
            >
              ← Previous
            </button>
            <span className="slide-count">
              Step {index + 1} of {steps.length}
            </span>
            <div className="slide-track">
              <div
                className="slide-fill"
                style={{ width: `${((index + 1) / steps.length) * 100}%` }}
              />
            </div>
            <button
              className="btn btn-sm"
              onClick={() => setIndex((i) => Math.min(steps.length - 1, i + 1))}
              disabled={index >= steps.length - 1}
            >
              Next →
            </button>
          </div>
        </>
      )}

      {artifact.final_text && artifact.stop_reason === "NO_FUNCTION_CALL" && (
        <div className="prompt-box" style={{ marginTop: 14 }}>
          <b>Final model text</b> (stopped without calling submit_answer):
          <div style={{ marginTop: 6 }}>{artifact.final_text}</div>
        </div>
      )}
    </div>
  );
}
