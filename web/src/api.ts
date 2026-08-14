import type {
  AttemptDetail,
  Health,
  JobResult,
  Task,
} from "./types";

// Relative URLs: Vite proxies /api in dev, and FastAPI serves the built app in prod, so
// there is no base URL to configure in either mode.

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    // FastAPI puts validation failures in `detail`. Surfacing it verbatim means an
    // invalid task file tells the user exactly which task is wrong.
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/api/health"),

  listTasks: (tasksFile?: string | null) =>
    request<Task[]>(
      "/api/tasks" + (tasksFile ? `?tasks_file=${encodeURIComponent(tasksFile)}` : ""),
    ),

  uploadTasks: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ tasks_file: string; filename: string; tasks: Task[] }>(
      "/api/tasks/upload",
      { method: "POST", body: form },
    );
  },

  submitJob: (body: {
    task_ids: string[] | null;
    attempts_per_task: number;
    concurrency: number;
    tasks_file?: string | null;
    max_steps?: number | null;
    timeout_s?: number | null;
    record_video?: boolean;
  }) =>
    request<JobResult>("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  listJobs: () => request<JobResult[]>("/api/jobs"),

  getJob: (jobId: string) => request<JobResult>(`/api/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    request<{ cancelled: boolean; running: boolean }>(
      `/api/jobs/${jobId}/cancel`,
      { method: "POST" },
    ),

  getAttempt: (jobId: string, attemptId: string) =>
    request<AttemptDetail>(`/api/jobs/${jobId}/attempts/${attemptId}`),

  /** URL for a screenshot or video inside an attempt directory. */
  mediaUrl: (jobId: string, attemptId: string, path: string) =>
    `/api/jobs/${jobId}/attempts/${attemptId}/media/${path}`,
};
