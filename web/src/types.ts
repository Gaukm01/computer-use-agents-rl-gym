// Mirrors rlgym/schemas.py. Kept hand-written and small rather than generated, because
// the UI only needs the fields it renders -- the artifacts carry considerably more.

export type AttemptStatus =
  | "QUEUED"
  | "IN_PROGRESS"
  | "COMPLETED_CORRECT"
  | "COMPLETED_INCORRECT"
  | "MAX_STEPS_EXCEEDED"
  | "TIMED_OUT"
  | "ERRORED";

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
  /** null means the outcome was never observed (ERRORED) -- not a zero. */
  reward: number | null;
  verdict: string | null;
  near_miss: boolean;
  step_count: number;
  duration_s: number | null;
  total_tokens: number;
  used_sql_editor: boolean;
  error: string | null;
  diff: string | null;
  has_video: boolean;
  screenshot_count: number;
}

export interface TaskResult {
  task_id: string;
  prompt: string | null;
  expected_output: unknown;
  attempts: AttemptSummary[];
  total_attempts: number;
  valid_attempts: number;
  passed: number;
  failed: number;
  errored: number;
  pass_at_k: boolean;
  pass_rate: number | null;
  /** Errors left fewer valid attempts than requested, so pass@k rests on fewer samples. */
  low_confidence: boolean;
}

export interface JobSpec {
  job_id: string;
  task_ids: string[];
  tasks_file: string | null;
  attempts_per_task: number;
  concurrency: number;
  gym_url: string;
  created_at: string | null;
  agent_config: { model: string; hint: string | null };
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
  valid_attempts: number;
  passed: number;
  failed: number;
  errored: number;
  pass_at_k_rate: number | null;
  mean_reward: number | null;
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
  safety_acknowledged: boolean;
  latency_ms: number;
}

export interface AttemptArtifact {
  attempt_id: string;
  task_id: string;
  attempt_index: number;
  status: AttemptStatus;
  stop_reason: string | null;
  duration_s: number | null;
  steps: StepRecord[];
  step_count: number;
  actual_output: unknown;
  actual_output_raw: string | null;
  final_text: string | null;
  error: string | null;
  video_path: string | null;
  used_sql_editor: boolean;
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

export interface Health {
  ok: boolean;
  gym_reachable: boolean;
  /** The cookie file exists. Says nothing about whether it still works. */
  auth_captured: boolean;
  /** The cookie actually authenticates. null = couldn't check (usually the gym is down). */
  auth_valid: boolean | null;
  /** Logged-in account when healthy, otherwise why the check failed. */
  auth_detail: string | null;
  tasks_available: number;
}
