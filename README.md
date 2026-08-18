# Metabase Computer-Use RL Environment

A reinforcement-learning environment for computer-use agents. A Gemini computer-use model is
given an analyst-style question, sees nothing but screenshots of a real Metabase instance
seeded with realistic environment data, drives it with a mouse and keyboard, and is graded on
whether the answer it submits is correct.

Three parts, which is also how the code is organised:

| Part | What it is | Where |
|---|---|---|
| **The gym** | Metabase + Postgres, restored from the provided environment dump, one private stack per rollout | `rlgym/gym_stack.py`, `metabase/docker-compose.yml` |
| **The agent** | Gemini computer-use driving a real Chromium via Playwright | `rlgym/agents/gemini_cu.py`, `rlgym/computer.py` |
| **The infra** | Task ingestion, parallel rollouts, grading, artifacts, web console | `rlgym/orchestrator.py`, `rlgym/service.py`, `server/`, `web/` |

**The one hard constraint: the agent is visual-only.** It receives screenshots and emits
clicks and keystrokes. It never gets a REST API, a SQL connection, or DOM access. The
*harness* does use Metabase's REST API — to mint a session and to check we are not sitting on
a login page — because the harness is not the policy.

**Deliverable documents**

- [`deliverables/DESIGN.md`](deliverables/DESIGN.md) — what we built and how it works.
- [`deliverables/PRODUCTION.md`](deliverables/PRODUCTION.md) — the plan to productionise it.

---

## How one rollout works

```
POST /api/jobs  →  a job is T tasks x K attempts
   │
   └─ thread pool (concurrency, capped at 3)
        └─ ONE ATTEMPT = one subprocess + one private gym
             ├─ claim a host port (3001-3003)                    phase=PROVISIONING
             ├─ docker compose up postgres ONLY
             ├─ pg_restore resources/metabase_envdata.sql
             ├─ docker compose up metabase, poll /api/health
             ├─ POST /api/session, write storage_state.json      phase=AUTHENTICATING
             ├─ verify the seeded state restored correctly
             ├─ launch Chromium, verify the cookie works         phase=CONNECTING
             ├─ agent loop: screenshot → model → action          phase=RUNNING
             │    artifact.json is rewritten after EVERY step
             ├─ agent calls submit_answer, episode ends
             ├─ destroy the stack, verify it is gone, fstrim
             └─ grade: compare against the task's answer         phase=DONE  →  reward 1 / 0 / null
```

Every rollout gets a fresh Metabase. Nothing is shared and nothing is reused, so every rollout
starts from identical state.

---

## Setup

Seven steps. Verified on macOS (Apple Silicon, colima); Linux with native Docker works the
same way minus step 1's colima specifics.

### Prerequisites

**macOS** — via [Homebrew](https://brew.sh):

```bash
# skip this line if you already have Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install colima docker docker-compose    # Docker daemon, CLI, and `docker compose`
brew install python@3.12
brew install node                            # or: nvm install 18 && nvm use 18
```

**Linux** — install `docker`, `docker-compose-plugin`, `python3.12` and `nodejs` (18+) from
your package manager. colima is not needed; skip it in step 1.

Confirm all three before continuing. Node **must** be 18+ — Vite 5 fails on 14/16:

```bash
docker --version && python3.12 --version && node --version
```

Also needed:

- **~10 GiB free host disk**, and **≥8 GiB RAM available to the Docker VM**
- A **Gemini API key** ([aistudio.google.com/apikey](https://aistudio.google.com/apikey))
- **Metabase credentials** for a user that exists inside the provided environment dump

### 1. Start the Docker runtime

Each rollout holds its own Metabase (~1.1 GiB RSS) plus a Postgres (~55 MiB), and up to three
run at once, so the VM needs real memory:

```bash
colima start --cpu 4 --memory 8 --disk 60      # macOS; skip on native Linux
docker info                                     # must succeed before continuing
```

### 2. Clone the vendored computer-use agent

It is deliberately git-ignored — it has its own history and we treat it as a read-only
reference we subclass:

```bash
cd agent && git clone --depth 1 https://github.com/google-gemini/computer-use-preview.git && cd ..
```

> If you received this project as a ZIP rather than a git clone, the directory is already
> there. Skip this step.

### 3. Create the Python environment

```bash
python3.12 -m venv agent/.venv
agent/.venv/bin/pip install -U pip
agent/.venv/bin/pip install -r requirements.txt
agent/.venv/bin/playwright install chromium
```

`requirements.txt` is the single, complete dependency list — it already includes everything
the vendored clone needs, because that clone is not present in a fresh git checkout.

### 4. Create `.env` at the repository root

```ini
# ---- Gemini ----
GEMINI_API_KEY=your-key-here

# MUST stay empty. google-genai raises
#   "Project/location and API key are mutually exclusive"
# if any of these is set alongside an API key.
USE_VERTEXAI=
VERTEXAI_PROJECT=
VERTEXAI_LOCATION=

# ---- Metabase ----
# A user that exists in resources/metabase_envdata.sql. Used only by the harness, to mint a
# session per rollout. The agent never sees these: attempts start pre-authenticated via a
# cookie, so the password never enters model context or a transcript.
METABASE_EMAIL=you@example.com
METABASE_PASSWORD=your-password
METABASE_URL=http://localhost:3000
```

### 5. Pre-pull the Docker images

```bash
docker pull postgres:16.9
docker pull metabase/metabase:v0.54.15
```

Not optional in practice: a cold pull *inside* a rollout stalls provisioning for minutes and
reads as a hang, which is why `/api/health` checks for both images explicitly.

> Metabase is pinned to **v0.54.15** on purpose. The provided dump is a v0.54-era app
> database; booting v0.55+ against it would migrate the schema forward and permanently alter
> the seeded environment.

### 6. Build the console

```bash
cd web && npm install && npm run build && cd ..
```

This emits `web/dist`, which the API serves itself — one process for the whole demo. For
frontend development instead, `npm run dev` serves on `:5173` and proxies `/api` to `:8000`.

### 7. Run it

```bash
agent/.venv/bin/uvicorn server.app:app --port 8000
```

Open **http://localhost:8000**. If setup is incomplete the console shows a red banner naming
exactly what is missing; otherwise there is no banner.

**Ports used:** `8000` API and console · `3001-3003` rollout gyms · `3000` reserved for a
manual dev gym · `5173` Vite dev server.

---

## Verify the install

```bash
agent/.venv/bin/python -m pytest tests -q       # expect: 111 passed, 7 skipped
curl -s localhost:8000/api/health               # expect: every flag true, detail null
```

The 7 skips need a manual gym on `:3000` (see below); they are not failures.

---

## Running rollouts

### From the console (the intended path)

1. Optionally **upload a `tasks.json`** — it is validated before it is stored, and the stored
   file is what the job actually runs. Otherwise the bundled `resources/tasks.json` is used.
2. Pick tasks, set **attempts per task** (K) and **concurrency** (max 3).
3. **Run.** The job page polls every 2 s and shows each rollout's phase while it provisions.
4. Click any finished rollout to open its transcript: per-step screenshot, model reasoning,
   the action it took, and the grade with expected vs actual.

### From the CLI

```bash
# one rollout, verbose, ~4-5 min
agent/.venv/bin/python -m rlgym.scripts.run_one --task problem10
agent/.venv/bin/python -m rlgym.scripts.run_one --task problem1 --headed   # watch the browser

# a full job: 3 attempts each of two tasks, 3 in parallel
agent/.venv/bin/python -m rlgym.scripts.run_job --tasks problem1,problem3 --attempts 3 --concurrency 3

# every task, one attempt each
agent/.venv/bin/python -m rlgym.scripts.run_job

# continue an interrupted job -- finished rollouts are not re-run
agent/.venv/bin/python -m rlgym.scripts.run_job --resume job-20260817-140645-64909b
```

### A gym you can click around in yourself

```bash
agent/.venv/bin/python -m rlgym.gym_stack --port 3000 --keep     # Ctrl-C tears it down
```

### What a run costs

A rollout averages **~320 s** and **~560k tokens** (prompt:output ≈ 520:1 — screenshots
dominate completely), and both scale with the step cap. Budget accordingly:

| Job | Rollouts | Wall clock @ concurrency 3 | Tokens |
|---|---|---|---|
| 9 tasks × 5, 60-step cap | 45 | ~90 min | ~25 M |
| 1 task × 10, 80-step cap | 10 | ~32 min | ~13 M |

Jobs are designed to run **one at a time** — starting a job sweeps orphaned rollout
containers, so a second concurrent job would remove the first job's gyms.

---

## Where results land

```
runs/<job_id>/
├── job.json                        # live job state: every T x K row, pass@k, stats
└── <task>-a<k>-<uuid>/
    ├── artifact.json               # the full trajectory: every step, reasoning, tokens, status
    ├── evaluation.json             # verdict, reward, diff, evaluator_version
    ├── spec.json                   # exactly what this rollout was asked to do
    ├── steps/step_NNN_<action>.png # one screenshot per step
    ├── video/<hash>.webm           # the whole episode
    └── worker.log
```

`artifact.json` is authoritative and is rewritten after every step, so a killed rollout still
keeps its trajectory. `job.json` is a view over those artifacts and is recomputed on read.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Red banner: *"Attempts will fail until this is fixed"* | It names the cause — Docker down, an image not pulled, dump missing, or credentials unset. |
| `ImportError: cannot import name 'BrowserAgent'` | Step 2 was skipped: `agent/computer-use-preview/` is missing. |
| `ValueError: Project/location and API key are mutually exclusive` | A `VERTEXAI_*` variable is set in `.env`. All three must be empty. |
| `port already allocated` | Rollout containers left over from a Ctrl-C. A new job sweeps them automatically; to do it by hand: `docker ps -aq --filter label=rlgym.ephemeral \| xargs docker rm -f` |
| `metabase did not answer /api/health within 420s` | The VM is short of memory for three JVMs. Raise `--memory` or drop concurrency to 1-2. |
| `No space left on device` | colima's disk image is sparse and only grows. We `fstrim` after every teardown (host growth measured at 0.8 MB/rollout), but if it ever climbs, `colima restart` reclaims it. |
| `npm run build` fails on syntax | Node is older than 18. |
| A job shows `RUNNING` with nothing happening | The API was restarted mid-job. Artifacts survive; resume it with `run_job --resume <job_id>`. |
| `problem2` fails every time | Expected — its provided ground truth is not reproducible in this sample database. See `deliverables/DESIGN.md`. |

---

## Repository layout

```
rlgym/                  the platform
├── gym_stack.py        one ephemeral Metabase+Postgres stack per rollout
├── ports.py            host-port allocator; the queue IS the concurrency semaphore
├── computer.py         Playwright browser: pre-authenticated, recorded, domain-locked
├── agents/gemini_cu.py the Gemini computer-use loop
├── executor.py         run ONE rollout: provision, subprocess, wall clock, classify
├── grader.py           the reward function; a pure function of (artifact, task)
├── orchestrator.py     run a job: T x K, in parallel, resumable
├── service.py          everything the platform can do, with no web framework in sight
├── schemas.py          the artifact wire format, and how reward derives from status
└── scripts/            run_one, run_job
server/app.py           FastAPI: thin routes over service.py, and serves web/dist
web/src/                React console: NewRun, JobView, Transcript
metabase/               docker-compose.yml for one ephemeral gym
resources/              tasks.json, metabase_envdata.sql
tests/                  111 tests, no Docker required
runs/                   rollout artifacts (git-ignored)
```
