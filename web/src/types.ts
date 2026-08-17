// Mirrors rlgym/schemas.py. Kept hand-written and small rather than generated, because
// the UI only needs the fields it renders -- the artifacts carry considerably more.

export type AttemptStatus =
  | "QUEUED"
  | "IN_PROGRESS"
  | "COMPLETED_CORRECT"   // CORRECT — wire value kept from before the rename
  | "ANSWERED"            // an answer exists and is not correct (or none was given)
  | "MAX_STEPS_EXCEEDED"
  | "TIMED_OUT"
  | "NOT_OBSERVED";       // infrastructure failure — reward null, out of pass@k

/** How far an attempt got. Orthogonal to status; never drives reward. */
export type AttemptPhase =
  | "QUEUED"
  | "PROVISIONING"
  | "AUTHENTICATING"
  | "CONNECTING"
  | "RUNNING"
  | "GRADING"
  | "DONE";

export type JobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "CANCELLED" | "FAILED";

export interface Task {
  id: string;
  task: string;
  answer: string;
}

export interface AttemptSummary {
  attempt_id: string;
  task_id: string;
  attempt_index: number;
  status: AttemptStatus;
  phase: AttemptPhase;
  /** null means the outcome was never observed (NOT_OBSERVED) -- not a zero. */
  reward: number | null;
  verdict: string | null;
  near_miss: boolean;
  step_count: number;
  duration_s: number | null;
  total_tokens: number;
  error: string | null;
  diff: string | null;
  has_video: boolean;
  screenshot_count: number;
}

/** Cost of a set of attempts. Averages cover *observed* attempts only -- one that died
 *  in provisioning contributes 0 steps and would drag them toward zero. */
export interface RunStats {
  observed_attempts: number;
  total_tokens: number;
  total_duration_s: number;
  avg_duration_s: number | null;
  avg_steps: number | null;
  total_steps: number;
}

export interface TaskResult {
  task_id: string;
  prompt: string | null;
  expected_output: unknown;
  /** One row per REQUESTED attempt, including ones that have not started -- those carry
   *  an empty attempt_id, which is what makes them unopenable. */
  attempts: AttemptSummary[];
  total_attempts: number;
  completed: number;
  running: number;
  queued: number;
  valid_attempts: number;
  passed: number;
  failed: number;
  errored: number;
  pass_at_k: boolean;
  pass_rate: number | null;
  stats: RunStats;
}

export interface AgentConfig {
  agent_type: string;
  model: string;
  hint: string | null;
  temperature: number;
  top_p: number;
  top_k: number;
  /** Reporting remaining budget to the agent. Off, and the platform never sets it. */
  budget_reminders: boolean;
  prohibited_actions: string[];
}

export interface JobSpec {
  job_id: string;
  task_ids: string[];
  tasks_file: string | null;
  attempts_per_task: number;
  concurrency: number;
  created_at: string | null;
  agent_config: AgentConfig;
  limits: { max_steps: number; timeout_s: number };
}

export interface JobResult {
  spec: JobSpec;
  status: JobStatus;
  started_at: string | null;
  ended_at: string | null;
  duration_s: number | null;
  tasks: TaskResult[];
  total_attempts: number;
  completed_attempts: number;
  running_attempts: number;
  queued_attempts: number;
  valid_attempts: number;
  passed: number;
  failed: number;
  errored: number;
  pass_at_k_rate: number | null;
  mean_reward: number | null;
  stats: RunStats;
  total_tokens: number;
  error: string | null;
}

export interface StepRecord {
  index: number;
  action_name: string;
  action_args: Record<string, unknown>;
  reasoning: string | null;
  screenshot: string | null;
  url: string | null;
  refused: boolean;
  refused_reason: string | null;
  /** The model emitted a call the tool schema couldn't satisfy. Distinct from `refused`:
   *  a refusal is our boundary, this is the agent's own mistake. */
  action_error: string | null;
  safety_acknowledged: boolean;
  latency_ms: number;
}

export interface AttemptArtifact {
  attempt_id: string;
  task_id: string;
  attempt_index: number;
  status: AttemptStatus;
  phase: AttemptPhase;
  pristine_counts: Record<string, number> | null;
  stop_reason: string | null;
  duration_s: number | null;
  steps: StepRecord[];
  step_count: number;
  actual_output: unknown;
  actual_output_raw: string | null;
  /** The exact first turn the agent was given: environment preamble, task, and the agent's
   *  own answer contract. Not a system prompt -- it is sent as a user turn. */
  initial_prompt: string | null;
  final_text: string | null;
  error: string | null;
  video_path: string | null;
  refusals: string[];
  safety_acknowledgements: number;
  token_usage: { prompt_tokens: number; output_tokens: number; total_tokens: number };
}

export interface EvaluationResult {
  attempt_id: string;
  task_id: string;
  expected_output: unknown;
  actual_output: unknown;
  verdict: "pass" | "fail" | "not_graded";
  reward: number | null;
  diff: string | null;
  near_miss: boolean;
  evaluator_version: string;
}

export interface AttemptDetail {
  artifact: AttemptArtifact;
  evaluation: EvaluationResult | null;
  screenshots: string[];
  video: string | null;
}

/** No long-lived gym to probe -- each attempt provisions its own. These are the things
 *  that gate a run. */
export interface Health {
  ok: boolean;
  docker_available: boolean;
  images_present: boolean;
  dump_present: boolean;
  credentials_configured: boolean;
  tasks_available: number;
  max_gyms: number;
  /** Human-readable summary of whatever is missing. */
  detail: string | null;
}

/** Agent controls the UI offers, served so the form and the harness cannot drift. */
export interface AgentDefaults {
  models: string[];
  default: AgentConfig;
  max_gyms: number;
  timeout_s: number;
  max_steps: number;
}
