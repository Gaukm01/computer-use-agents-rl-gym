"""HTTP API for the rollout platform.

Deliberately thin. Every route is a call into `rlgym.service` plus serialization -- the
service layer already returns pydantic models, so FastAPI renders them directly and there
is no second set of DTOs to keep in sync. Anything with real logic in it belongs down in
the service, not here.

Run:
    agent/.venv/bin/uvicorn server.app:app --reload --port 8000

The built frontend is served from web/dist when it exists, so one process serves both.
During frontend development Vite runs separately on :5173 and proxies /api here.
"""

from __future__ import annotations

import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rlgym import service, vendor
from rlgym.schemas import HealthStatus, JobResult, Task

app = FastAPI(
    title="Metabase Computer-Use RL Gym",
    description="Run computer-use rollouts against the Metabase gym and grade them.",
    version="1.0.0",
)

# Vite's dev server is a different origin during frontend development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIST = vendor.PROJECT_ROOT / "web" / "dist"


# ======================================================================================
# Request / response models
# ======================================================================================


class SubmitJobRequest(BaseModel):
    task_ids: list[str] | None = Field(
        default=None, description="Which tasks to run. Omit for all tasks in the file."
    )
    attempts_per_task: int = Field(default=1, ge=1, le=20)
    concurrency: int = Field(default=3, ge=1, le=10)
    tasks_file: str | None = Field(
        default=None,
        description="Path returned by /api/tasks/upload. Omit for the bundled tasks.json.",
    )
    model: str | None = None
    hint: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    timeout_s: int | None = Field(default=None, ge=30, le=3600)
    gym_url: str = "http://localhost:3000"
    record_video: bool = True


class UploadTasksResponse(BaseModel):
    tasks_file: str
    filename: str
    tasks: list[Task]


# ======================================================================================
# Health
# ======================================================================================


@app.get("/api/health", response_model=HealthStatus)
def health(gym_url: str = service.DEFAULT_GYM_URL) -> HealthStatus:
    """The two things that make every run fail: the gym being down, and the auth cookie
    not working. Surfaced up front so nobody debugs a failed job when the real problem is
    setup.

    Note `auth_valid` is checked by actually using the cookie, not by checking the file
    exists — an expired session used to report healthy and then error every attempt.
    """
    return service.health(gym_url)


# ======================================================================================
# Tasks
# ======================================================================================


@app.get("/api/tasks", response_model=list[Task])
def list_tasks(tasks_file: str | None = None) -> list[Task]:
    try:
        return service.list_tasks(tasks_file)
    except FileNotFoundError:
        raise HTTPException(404, f"task file not found: {tasks_file}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


@app.post("/api/tasks/upload", response_model=UploadTasksResponse)
async def upload_tasks(file: UploadFile) -> UploadTasksResponse:
    """Accept a tasks.json and validate it before storing.

    The returned `tasks_file` is passed back on job submission, so an uploaded file
    genuinely drives the run rather than only changing what's displayed.
    """
    raw = await file.read()
    try:
        path, tasks = service.save_tasks_file(raw, file.filename or "tasks.json")
    except ValueError as exc:
        # A malformed upload is the user's mistake, not a server error -- say what's wrong.
        raise HTTPException(400, str(exc))
    return UploadTasksResponse(
        tasks_file=str(path), filename=path.name, tasks=tasks
    )


# ======================================================================================
# Jobs
# ======================================================================================


@app.post("/api/jobs", response_model=JobResult)
def submit_job(request: SubmitJobRequest) -> JobResult:
    """Start a job and return immediately. The UI then polls GET /api/jobs/{id}."""
    try:
        return service.submit_job(
            request.task_ids,
            attempts_per_task=request.attempts_per_task,
            concurrency=request.concurrency,
            model=request.model,
            hint=request.hint,
            max_steps=request.max_steps,
            timeout_s=request.timeout_s,
            gym_url=request.gym_url,
            record_video=request.record_video,
            tasks_file=request.tasks_file,
            block=False,
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc).strip("'"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/jobs", response_model=list[JobResult])
def list_jobs(limit: int = 50) -> list[JobResult]:
    return service.list_jobs(limit=limit)


@app.get("/api/jobs/{job_id}", response_model=JobResult)
def get_job(job_id: str) -> JobResult:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Stops scheduling new attempts. Running attempts finish so their artifacts and
    videos are written rather than truncated."""
    if service.get_job(job_id) is None:
        raise HTTPException(404, f"unknown job {job_id}")
    return {"cancelled": service.cancel_job(job_id), "running": service.is_running(job_id)}


# ======================================================================================
# Attempts
# ======================================================================================


@app.get("/api/jobs/{job_id}/attempts/{attempt_id}")
def get_attempt(job_id: str, attempt_id: str) -> dict[str, Any]:
    """Full trajectory for the transcript viewer: every step's action, the model's
    reasoning, screenshot paths, the video, and the verdict."""
    detail = service.get_attempt(job_id, attempt_id)
    if detail is None:
        raise HTTPException(404, f"unknown attempt {attempt_id} in job {job_id}")
    return detail


@app.get("/api/jobs/{job_id}/attempts/{attempt_id}/media/{path:path}")
def get_attempt_media(job_id: str, attempt_id: str, path: str) -> FileResponse:
    """Serve a screenshot or video from an attempt directory.

    Path traversal is rejected inside the service, so this route cannot be tricked into
    serving something outside the attempt's own directory.
    """
    resolved = service.attempt_file(job_id, attempt_id, path)
    if resolved is None:
        raise HTTPException(404, "not found")
    return FileResponse(resolved)


# ======================================================================================
# Static frontend (mounted last so it never shadows /api)
# ======================================================================================

if WEB_DIST.is_dir():

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
else:

    @app.get("/")
    def index_missing() -> dict[str, str]:
        return {
            "message": "API is up. Frontend not built.",
            "build": "cd web && npm install && npm run build",
            "dev": "cd web && npm run dev  (then open http://localhost:5173)",
            "docs": "/docs",
        }
