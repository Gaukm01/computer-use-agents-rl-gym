# Infra Knowledgebase

> **Looking for the short version?** Read [`DESIGN.md`](./DESIGN.md) instead — architecture,
> design decisions, and a condensed setup, in about ten minutes.
> **This file is the exhaustive reference**: every command, version, and gotcha. Come here
> when something breaks, when you need an exact command, or to rebuild from a clean machine.

Everything installed, configured, and run for this project — with the exact commands and
*why* each piece exists. Written so the whole stack can be rebuilt from a clean machine.

Host: macOS (Darwin 25.4.0), Apple Silicon (aarch64).
Project root: `/Users/gkumar/assessments/hackathon`

> **Status legend** — ✅ done & verified · 🟡 in progress · ⬜ planned, not built yet.
> Nothing marked ⬜ has been installed or run. No commands in those sections are verified.

---

## 1. Layout

```
hackathon/
├── metabase/
│   └── docker-compose.yml        # the gym: postgres (app DB) + metabase
├── agent/
│   ├── .venv/                    # python 3.12 env (NOT system 3.9) — runs everything
│   ├── computer-use-preview/     # vendored google-gemini reference clone (git-ignored)
│   ├── storage_state.json        # auth cookie — git-ignored, treat as a secret
│   ├── verify_gemini.py          # Phase 2: key live + scoped for computer use
│   └── verify_harness.py         # Phase 2: browser sees the gym
├── rlgym/                        # the rollout pipeline
│   ├── schemas.py                # artifact models + status→reward contract
│   ├── tasks.py                  # tasks.json loader
│   ├── computer.py               # GymComputer: pre-auth, video, domain lock
│   ├── capture_auth.py           # mints storage_state.json
│   ├── agents/base.py            # Agent protocol (agent-agnostic seam)
│   ├── agents/gemini_cu.py       # Gemini computer-use + submit_answer
│   ├── executor.py               # ONE attempt in a subprocess → artifact
│   ├── grader.py                 # versioned reward function
│   ├── orchestrator.py           # T×K attempts, parallel, resumable, pass@k
│   ├── service.py                # platform API surface (no web framework)
│   └── scripts/
│       ├── run_one.py            # one attempt — demo / debug
│       └── run_job.py            # a whole job
├── server/
│   └── app.py                    # FastAPI routes — thin wrappers over service.py
├── web/                          # React + TS eval console (Vite)
│   ├── src/NewRun.tsx            # upload tasks, pick tasks, set K, launch
│   ├── src/JobView.tsx           # job stats, task cards, attempt chips
│   ├── src/Transcript.tsx        # grade panel, per-step slides, video
│   ├── node_modules/ dist/       # git-ignored
│   └── package.json
├── tests/                        # 62 tests (pure ones run anywhere)
├── runs/<job_id>/                # job.json + one dir per attempt — git-ignored
├── uploads/                      # task files uploaded via the UI — git-ignored
├── resources/
│   ├── metabase_envdata.sql      # binary PGDMP dump — THE seed data (never edit)
│   ├── readable_metabase_envdata.sql  # plain-SQL twin, inspection only (never edit)
│   └── tasks.json                # 10 tasks: prompt + expected answer
├── requirements.txt              # platform deps (fastapi, uvicorn, multipart)
├── .env                          # secrets — git-ignored, never commit
├── .gitignore
├── DESIGN.md                     # architecture + decisions (start here)
├── PROGRESS.md                   # chronological log, Phases 1–5
└── INFRA_KNOWLEDGEBASE.md        # this file
```

**Status:** gym ✅ · agent toolchain ✅ · single attempt + grading ✅ · T×K parallel
orchestration ✅ · HTTP API + web console ✅ (Milestone 2 complete) ·
reset-between-rollouts and the Milestone 3 write-up ⬜ not built.

---

## 2. Host prerequisites ✅

### 2.1 What was already present

| Tool | Version | Notes |
|---|---|---|
| Homebrew | 6.0.17 | at `/opt/homebrew` |
| `libpq` (psql, pg_restore) | 18.4 | **keg-only** — see gotcha below |

### 2.2 What was installed

Nothing container-related existed on this machine — no Docker Desktop, OrbStack, Rancher,
Podman, or Colima. Installed:

```bash
brew install colima docker docker-compose
```

| Package | Version | Why |
|---|---|---|
| `colima` | 0.10.3 | Runs the Linux VM that hosts the Docker daemon. Apache-2.0, CLI-only, no GUI, no privileged helper, no license fee. |
| `docker` | 29.7.2 | Just the client CLI. Homebrew's `docker` formula has no daemon — Colima provides that. |
| `docker-compose` | 5.4.0 | Compose v2+ is a CLI *plugin*, not a standalone binary — hence the config step below. |

### 2.3 Post-install wiring (required)

Homebrew installs the compose plugin somewhere the docker CLI doesn't look by default.
Without this, `docker compose` reports "is not a docker command":

```bash
mkdir -p ~/.docker
cat > ~/.docker/config.json <<'EOF'
{
  "cliPluginsExtraDirs": [
    "/opt/homebrew/lib/docker/cli-plugins"
  ]
}
EOF
```

### 2.4 Gotcha: `libpq` is keg-only

Homebrew does not symlink `psql`/`pg_restore` onto `PATH`, so both appear "not installed".
They exist at `/opt/homebrew/opt/libpq/bin/`. Prepend it per-shell:

```bash
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"
```

To make it permanent: add that line to `~/.zshrc`.

---

## 3. Colima VM ✅

The Docker daemon runs inside a Linux VM. **It does not survive a reboot unless started.**
If `docker ps` errors with "Cannot connect to the Docker daemon", the VM is down.

```bash
# First-time / after reboot — 4 CPU, 8 GB RAM, 60 GB disk.
# Sized for Metabase's JVM (~2 GB) + Postgres with headroom.
colima start --cpu 4 --memory 8 --disk 60

colima status         # is it up?
colima stop           # free the RAM, keeps all data
colima delete         # NUKES the VM and every image/volume in it
```

Resource flags only apply at create time. To resize an existing VM:
`colima stop && colima start --cpu 6 --memory 12`.

Verified daemon: Docker Engine 29.5.2 on Ubuntu 24.04.4 LTS, aarch64, 4 CPU, 8 GB.

```bash
docker --version && docker compose version
docker info --format 'Server: {{.ServerVersion}} | Arch: {{.Architecture}}'
```

---

## 4. The gym stack ✅

Defined in `metabase/docker-compose.yml`. Two services, project name `metabase-gym`.

### 4.1 Ports

| Service | Container | Host | URL / use |
|---|---|---|---|
| metabase | 3000 | **3000** | http://localhost:3000 |
| postgres | 5432 | **5433** | `psql -h localhost -p 5433` |

Postgres is deliberately on **5433** to stay clear of any future local Postgres on 5432.

### 4.2 Credentials

**Postgres (app DB)** — arbitrary, chosen by us, local only:

| | |
|---|---|
| host / port | `localhost` / `5433` |
| user / password | `postgres` / `postgres` |
| database | `metabase_app` |

The user is `postgres` **on purpose**: every object in `metabase_envdata.sql` is owned by
`postgres`, so matching it avoids `role "postgres" does not exist` errors during restore.

**Metabase UI login** — local dev only:

| | |
|---|---|
| URL | http://localhost:3000 |
| email | `daksh@deeptune.com` |
| password | `Daksh@123` |

All 12 seeded users share one bcrypt hash, so this password should work for any of them
(`tim@`, `john@`, `shaily@`, `chirag@`, `happy@`, `heleina@`, `paul@`, `miller@`, `shawn@`,
`chris@` — all `@deeptune.com`, all superusers).

### 4.3 Why each Metabase env var

| Var | Value | Why |
|---|---|---|
| `MB_DB_TYPE` | `postgres` | **The critical one.** Default is embedded H2; our seed data is a Postgres dump and needs a Postgres target to restore into. |
| `MB_DB_HOST` | `postgres` | Compose service name, resolved on the internal network. |
| `MB_DB_PORT` | `5432` | Container-internal port — *not* the 5433 host mapping. |
| `MB_DB_DBNAME` | `metabase_app` | App DB. |
| `MB_DB_USER` / `MB_DB_PASS` | `postgres` / `postgres` | Matches dump ownership. |
| `JAVA_TIMEZONE` | `UTC` | Dump timestamps are `+00`; keeps report dates from shifting. |

### 4.4 Why Metabase is pinned to `v0.54.15`

Not arbitrary. The dump's newest Liquibase changeset is `v54.2025-03-28T11:22:01`, so the
app DB is **v0.54-era**, and `v0.54.15` was the current patch on the dump's creation date
(2025-07-09). Booting a newer minor (v0.55+) against this DB would auto-migrate the schema
forward and **permanently alter the seeded environment**. Patch releases within a minor add
no migrations, so this pin applies zero schema drift.

Tag timeline (from Docker Hub) used to pick it:

```
v0.54.5   2025-04-29     <- current when the dump's DB was first created (2025-05-01)
v0.54.15  2025-07-01     <- PINNED: current when the dump was taken (2025-07-09)
v0.54.19  2025-07-29     <- last of the v0.54 line before a long gap
```

### 4.5 Lifecycle commands

Run from `hackathon/metabase/`:

```bash
docker compose up -d            # start (pulls images on first run)
docker compose ps               # status + health
docker compose logs -f metabase # tail Metabase (JVM boot is slow, ~30-60s)
docker compose stop             # stop, keep data
docker compose down             # remove containers, KEEP the pgdata volume
docker compose down -v          # remove containers AND WIPE the app DB
docker compose up -d --force-recreate metabase   # apply env var changes
```

Metabase is not ready the moment the container starts — the JVM runs migrations first.
Block until genuinely up rather than guessing:

```bash
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done
curl -s http://localhost:3000/api/health     # => {"status":"ok"}
```

### 4.6 The sample dataset

The dashboards query Metabase's **own bundled H2 sample database** — there is no external
warehouse to provision. It is extracted from the JAR at startup to:

```
/plugins/sample-database.db.mv.db     # ~3.5 MB, alongside the driver JARs
```

Confirmed working on the vanilla boot: `select count(*) from ORDERS` → **18760 rows**.

⚠️ **Known path discrepancy.** The dump hardcodes the sample DB as
`file:/app/plugins/sample-database.db` — the *older* image layout, where `WORKDIR` was
`/app`. This image's `WORKDIR` is `/`, so the file lands at `/plugins/...` instead.
Forcing `MB_PLUGINS_DIR=/app/plugins` **does not work**: the JVM runs as a non-root user,
`/app` is root-owned `755`, so Metabase throws `AccessDeniedException` and silently falls
back to a temp dir. (`/plugins` is `777` in the image, which is why the default works.)
Metabase rewrites a moved sample-DB path on startup — see PROGRESS.md for the verified
post-restore behaviour rather than trusting that here.

---

## 5. Postgres access ✅

```bash
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"

# From the host (client 18.4 -> server 16.9)
PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d metabase_app

# From inside the container (client version matches server exactly — 16.9)
docker exec -it mb-postgres psql -U postgres -d metabase_app
```

Handy one-liners:

```bash
# list tables
PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d metabase_app -c '\dt'

# what the gym contains
PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d metabase_app -c \
  "select name from report_dashboard order by id;"
```

---

## 6. Restoring the seed data ✅

Full verification output in `PROGRESS.md`. Source of truth is the **binary** dump;
`pg_restore` gets dependency ordering (extensions, sequences, FKs) right where plain SQL
through `psql` can fail on ordering.

Dump facts, verified: PGDMP custom format, gzip, dump version 1.15-0, source db `root_db`,
from PostgreSQL 16.9, created 2025-07-09, 1098 TOC entries, needs the `citext` extension
(bundled in the official `postgres` image).

`resources/` is bind-mounted **read-only** at `/resources` inside `mb-postgres`, so the
dumps can never be modified in place, and it gives an in-container restore path whose client
version matches the server exactly.

```bash
cd /Users/gkumar/assessments/hackathon

# 0. Inspect without restoring (read-only, always safe)
/opt/homebrew/opt/libpq/bin/pg_restore --list resources/metabase_envdata.sql | head -40

# 1. Metabase must be down — it holds open connections to the app DB
docker compose -f metabase/docker-compose.yml stop metabase

# 2. The target must be EMPTY. Restoring over the vanilla schema collides on every table.
docker exec mb-postgres psql -U postgres -d postgres \
  -c "DROP DATABASE IF EXISTS metabase_app;" \
  -c "CREATE DATABASE metabase_app OWNER postgres;"

# 3. Restore — exit code 0, zero errors
docker exec mb-postgres pg_restore -U postgres -d metabase_app /resources/metabase_envdata.sql

# 4. Back up against the restored data
docker compose -f metabase/docker-compose.yml up -d metabase
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done
```

### 6.1 Use the in-container `pg_restore`, not the host one

Both work, but the host client is version 18.4 against a 16.9 server, which logs:

```
pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter "transaction_timeout"
pg_restore: warning: errors ignored on restore: 1
```

`transaction_timeout` is a Postgres **17+** session setting that the newer client emits and
the 16.9 server rejects. It is harmless — it only disables a timeout, and row counts are
identical either way — but the in-container client (16.9, exact match) restores with zero
errors and is the cleaner thing to script.

### 6.2 Expected post-restore state

```
tables: 92              collection: 14
core_user: 12           metabase_table: 8
report_dashboard: 6     query_execution: 2997
report_card: 86         databasechangelog: 475  (pre-boot; becomes 480, see below)
report_dashboardcard: 81
```

### 6.3 Metabase applies 5 migrations on first boot — expected

`databasechangelog` goes **475 → 480**. Verified as safe:

| changeset | effect |
|---|---|
| `v52.2025-05-28T00:00:01` | download-results → `no` where view-data blocked. **No-op here** — `data_permissions` stayed byte-identical to the dump. |
| `v53.2025-05-06T16:03:19` | index values with bigint ids |
| `v54.2025-05-13T21:46:53/54/55` | search-index metadata: language, replace index, concurrency guard |

No seeded content is altered, and the same 5 apply on every fresh restore, so resets stay
deterministic. These changesets are dated May 2025 yet absent from a July dump, which means
the source instance actually ran **~v0.54.5** and was never upgraded — pin `v0.54.5` if you
ever need a literally zero-migration restore.

### 6.4 The sample-DB path self-heals — verified

The `/app/plugins` vs `/plugins` discrepancy from §4.6 needs **no workaround**. On boot
Metabase re-extracts the file and rewrites the stored path:

```
metabase.sample-data :: Loading sample database
util.files :: Extract file /sample-database.db.mv.db -> /plugins/sample-database.db.mv.db
```

`metabase_database.details` changes from `file:/app/plugins/sample-database.db` to
`file:/plugins/sample-database.db` on its own.

### 6.5 Verifying the gym actually works

Structural checks aren't enough — execute a seeded card:

```bash
SID=$(curl -s -X POST http://localhost:3000/api/session -H 'Content-Type: application/json' \
      -d '{"username":"daksh@deeptune.com","password":"Daksh@123"}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -H "X-Metabase-Session: $SID" http://localhost:3000/api/dashboard        # 6 dashboards
curl -s -X POST -H "X-Metabase-Session: $SID" -H 'Content-Type: application/json' \
     http://localhost:3000/api/dashboard/2/dashcard/37/card/9/query -d '{}'      # real rows
```

Known-good results: `'Revenue'` → 12 rows, `'Revenue by state'` → 48 rows,
`'Revenue by product category'` → 4 rows.

### 6.6 Benign log noise

~25 lines match `/ERROR/` in a healthy boot. They're Quartz `INFO` lines resetting restored
job triggers from `ERROR` state to `WAITING` — expected, since the dump captured those
scheduler triggers mid-state. Not a failure signal.

---

## 7. Computer-use agent ✅ WIRED AND PASSING

**Locked decision: visual only.** The agent interacts with Metabase exclusively through
screenshots and synthetic mouse/keyboard actions. No REST API calls, no direct DB queries, no
Metabase API client — the point is testing computer-use capability, not scripted automation.
The one narrow exception is *verification tooling*: `verify_harness.py` reads the DOM to
assert the login page really rendered. The agent itself never does.

### 7.1 Python environment ✅

```bash
cd /Users/gkumar/assessments/hackathon/agent
/opt/homebrew/bin/python3.12 -m venv .venv
./.venv/bin/pip install -r computer-use-preview/requirements.txt
./.venv/bin/playwright install chromium
```

**Do not use the system Python** (`/usr/bin/python3`, 3.9.6) — the repo's CI matrix is
`3.10 / 3.11 / 3.x`. Homebrew has 3.12.13.

Resolved versions: `google-genai 2.8.0`, `playwright 1.55.0`, `pydantic 2.12.0`,
Chromium 140.0.7339.16.

### 7.2 Secrets ✅

`.env` at the **project root** (not in the vendored repo), git-ignored. `python-dotenv`
walks up from the working directory, so both our scripts and the repo's `main.py` find it.
Holds `GEMINI_API_KEY`. Verified live: all 5 computer-use-capable models reachable.

⚠️ **`VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` must be left EMPTY.** `agent.py` passes them
to `genai.Client()` unconditionally, even when `USE_VERTEXAI=false`, and google-genai raises:

```
ValueError: Project/location and API key are mutually exclusive in the client initializer.
```

Upstream's `.env.example` ships `VERTEXAI_LOCATION=us-central1`, so copying it verbatim and
adding an API key fails immediately. Setting them blank is the fix — no code change needed.

⚠️ **`PLAYWRIGHT_HEADLESS` is a truthiness trap.** `bool(os.environ.get(...))` tests the
string, so `PLAYWRIGHT_HEADLESS=false` gives `headless=True`. Leave it **empty or unset** for
a headed (visible) browser; set it to any non-empty value for headless.

### 7.3 Verification scripts ✅

| script | proves | touches Gemini? | touches Metabase? |
|---|---|---|---|
| `agent/verify_gemini.py` | key is valid *and* scoped for computer use | yes | no |
| `agent/verify_harness.py` | harness renders the gym, PNG format/size correct | no | yes (browser only) |

```bash
cd agent
./.venv/bin/python verify_gemini.py
PLAYWRIGHT_HEADLESS=1 ./.venv/bin/python verify_harness.py   # writes artifacts/step7_login_page.png
```

### 7.4 Running the upstream agent loop (gated — ask before running)

The repo's `main.py` is a complete runnable agent. Pointing it at the gym requires no code
from us:

```bash
colima start                                              # VM does not survive reboot
cd /Users/gkumar/assessments/hackathon/metabase && docker compose up -d

cd /Users/gkumar/assessments/hackathon/agent/computer-use-preview
../.venv/bin/python main.py \
  --query "<task>" \
  --env playwright \
  --initial_url "http://localhost:3000" \
  --highlight_mouse
```

- `--initial_url` **must** include `http://` — `navigate()` prepends `https://` otherwise.
- `--highlight_mouse` draws a red circle at each click. Useful for watching, adds ~1s/action.
- `--model` defaults to `gemini-3.6-flash`; see §12.2 for which action set that selects.
- With no reset flow built yet, keep demo tasks **read-only** or be ready to re-restore.

### 7.5 Running a rollout ✅

Three things must be true before any attempt: Colima up, gym healthy, auth cookie captured.

```bash
colima start                                              # VM does not survive reboot
cd /Users/gkumar/assessments/hackathon/metabase && docker compose up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done

cd /Users/gkumar/assessments/hackathon
agent/.venv/bin/python -m rlgym.capture_auth               # once; re-run if session expires

# one attempt, one task
agent/.venv/bin/python -m rlgym.run_one --task problem10
```

Useful flags:

| Flag | Effect |
|---|---|
| `--headed` | Show the browser window. Best way to watch what the agent is doing. |
| `--max-steps N` | Override the 40-step budget. `--max-steps 1` is a fast plumbing check. |
| `--timeout S` | Wall-clock budget (default 600s). |
| `--model M` | Any of the 5 reachable computer-use models. |
| `--hint "..."` | Extra guidance appended to the prompt; recorded for ablation grouping. |
| `--no-video` | Skip video recording. |

**Exit code is 1 only for `ERRORED`.** A wrong answer is a valid outcome and must not look
like a broken pipeline to a caller or CI step.

### 7.6 Running a job (many rollouts, in parallel)

```bash
# every task, 1 attempt each -- the cheap default
agent/.venv/bin/python -m rlgym.scripts.run_job

# pass@3 over three tasks
agent/.venv/bin/python -m rlgym.scripts.run_job \
    --tasks problem10,problem5,problem6 --attempts 3 --concurrency 3

# cheap smoke test of the whole pipeline: ~1 API call per attempt
agent/.venv/bin/python -m rlgym.scripts.run_job \
    --tasks problem10,problem5 --attempts 2 --max-steps 1 --no-video

# continue an interrupted job -- finished attempts are NOT re-run
agent/.venv/bin/python -m rlgym.scripts.run_job --resume job-20260815-...
```

| Flag | Effect |
|---|---|
| `--tasks a,b,c` | Which tasks. Default: all 10. |
| `--attempts K` | Attempts per task. **Default 1**; use 3+ for pass@k. |
| `--concurrency N` | Parallel attempts, default 3. Bounded by API rate limits, not RAM. |
| `--resume JOB_ID` | Continue a job; terminal attempts are skipped. |
| `--max-steps` / `--timeout` | Per-attempt budgets, as for a single run. |

⚠️ **Cost.** A real attempt is ~500k tokens and ~4–5 min. All 10 tasks × 3 attempts is
~15M tokens and roughly 40 min at concurrency 3. Use `--max-steps 1` when testing plumbing.

Exit code is non-zero only if the *orchestrator* failed. Tasks failing is a valid result.

### 7.7 Job artifacts

```
runs/<job_id>/
├── job.json                    live job state -- rewritten after every attempt
└── <attempt_id>/               one directory per attempt (same layout as §7.8 below)
```

`job.json` is the polling surface: a UI reads it on an interval to follow a running job. It
carries per-task aggregates (`passed`/`failed`/`errored`, `pass_at_k`, `pass_rate`,
`low_confidence`) plus a compact `AttemptSummary` per attempt. Full trajectories stay in the
per-attempt `artifact.json` and are fetched only when someone opens one.

### 7.8 Where attempt artifacts land

```
runs/<job_id or "adhoc">/<attempt_id>/
├── artifact.json      full trajectory: status, steps, tokens, refusals
├── evaluation.json    verdict, reward, diff, evaluator_version
├── spec.json          exact inputs -- makes the attempt reproducible
├── steps/             step_000_click.png, ... one screenshot per action
├── video/*.webm       full session recording
└── worker.log         subprocess stdout/stderr -- first place to look on ERRORED
```

`artifact.json` is rewritten after **every step**, so a killed attempt keeps its partial
trajectory and a UI can follow a run live.

### 7.9 Attempt failure modes and what they mean

| Status | Meaning | What to do |
|---|---|---|
| `COMPLETED_CORRECT` | Answer matched. reward 1 | — |
| `COMPLETED_INCORRECT` | Submitted a wrong answer, or stopped without submitting. reward 0 | Read `evaluation.json` → `diff` |
| `MAX_STEPS_EXCEEDED` | Ran out of steps. reward 0 | Check the action sequence for a loop; consider `--max-steps` |
| `TIMED_OUT` | Ran out of wall clock. reward 0 | Usually a slow gym or a stuck page |
| `ERRORED` | **Infra failure. reward null, excluded from pass@k** | See below |

Common `ERRORED` causes, all verified:

```
"GEMINI_API_KEY missing from .env"                  -> add the key
401 UNAUTHENTICATED                                 -> key invalid or revoked
"not authenticated after injecting storage_state"   -> python -m rlgym.capture_auth
"no storage_state at ..."                           -> python -m rlgym.capture_auth
"worker produced no artifact"                       -> read worker.log; usually an import error
```

### 7.10 Tests

```bash
agent/.venv/bin/python -m pytest tests/ -q          # 62 tests
```

`tests/test_grader.py` is pure logic and runs anywhere. `tests/test_computer.py` needs the
gym plus a captured cookie and **skips cleanly** without them, so the suite stays green on a
fresh checkout.

### 7.11 The service layer (what the HTTP API calls)

`rlgym/service.py` imports no web framework on purpose, so the API becomes pure wiring and
the same functions stay usable from a CLI, a notebook or a test.

| Function | Purpose |
|---|---|
| `list_tasks()` / `get_task(id)` | Ingest `tasks.json` for the task picker |
| `submit_job(task_ids, attempts_per_task, concurrency, ...)` | Start a job; returns immediately unless `block=True` |
| `get_job(job_id)` / `list_jobs()` | Poll state; newest first |
| `resume_job(job_id)` | Continue after an interruption |
| `cancel_job(job_id)` / `is_running(job_id)` | Stop scheduling new attempts |
| `get_attempt(job_id, attempt_id)` | Full trajectory + verdict + media paths |
| `attempt_screenshots` / `attempt_video` / `attempt_file` | Media for the transcript viewer |

`attempt_file()` rejects path traversal in the service rather than at the route, so every
caller is protected rather than each endpoint having to remember.

**Known limitation, deliberate:** the registry of *running* jobs is in memory, so restarting
the API abandons in-flight jobs. Artifacts and `job.json` survive and `resume_job()` picks
them up. Moving that registry to a queue or DB is the Milestone 3 answer, not something to
fake now.

### 7.12 The web platform (API + UI)

**Start it** — one process serves both the API and the built UI:

```bash
cd /Users/gkumar/assessments/hackathon
agent/.venv/bin/uvicorn server.app:app --port 8000
# open http://localhost:8000
```

**First time / after pulling**, install the platform deps and build the UI:

```bash
agent/.venv/bin/pip install -r requirements.txt          # fastapi, uvicorn, multipart

# Node 18+ required. The default node here is v14, which Vite cannot use:
export PATH="$HOME/.nvm/versions/node/v18.20.8/bin:$PATH"
cd web && npm install && npm run build && cd ..
```

**Frontend development** (hot reload) runs Vite separately; it proxies `/api` to :8000:

```bash
agent/.venv/bin/uvicorn server.app:app --reload --port 8000   # terminal 1
cd web && npm run dev                                          # terminal 2 → :5173
```

Interactive API docs are at http://localhost:8000/docs.

#### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | gym reachable, auth captured, task count |
| GET | `/api/tasks` | list tasks (optionally `?tasks_file=`) |
| POST | `/api/tasks/upload` | validate + store an uploaded tasks.json |
| POST | `/api/jobs` | submit a job; returns immediately |
| GET | `/api/jobs` | list jobs, newest first |
| GET | `/api/jobs/{id}` | job state — poll this to follow a run |
| POST | `/api/jobs/{id}/cancel` | stop scheduling new attempts |
| GET | `/api/jobs/{id}/attempts/{aid}` | full transcript + verdict |
| GET | `/api/jobs/{id}/attempts/{aid}/media/{path}` | screenshot or video |

#### Uploaded task files

Land in `uploads/` (git-ignored), **never** over `resources/tasks.json`. Validated before
being written, so a bad file fails immediately with a specific message rather than surfacing
later as a confusing job failure:

```
not valid JSON: ...                     expected a JSON array of tasks, got dict
task file is empty                      task 0 is missing required field(s): task, answer
task 'a': 'answer' must be a JSON string, but failed to parse: ...
duplicate task id 'a'
```

The stored path is recorded in the job spec, so an uploaded file genuinely drives the run.

#### UI structure

- **New eval run** — pick the task file (bundled or uploaded), select tasks, set K,
  concurrency, optional max-steps, then run. Warns about cost when the run is large.
- **Job view** — progress, pass@k, passed/failed/errored counts, then one card per task
  with attempt chips.
- **Transcript** — screenshot on the left, the agent's reasoning and the action it took on
  the right, one slide per step (arrow keys page through), plus a video tab and a GRADE
  panel with expected vs actual.

An ERRORED attempt is labelled "Errored", never "Failed", and its grade panel says the
reward is null and excluded from pass@k — the UI must not imply the agent got it wrong when
the outcome was never observed.

### 7.13 Still unwired ⬜

- **HTTP API**: FastAPI routes over `service.py` (§7.11). Pure wiring — the service layer
  already returns pydantic models, so routes are little more than serialization.
- **React + TS frontend**: submit jobs, browse transcripts. UI design deferred by agreement.
- **Reset between rollouts**: `TEMPLATE` approach decided (§8.2) but not implemented —
  genuinely unnecessary today because all 10 tasks are read-only.
- **Milestone 3 write-up**: productionization design.
- Whether the browser should move into a container beside Metabase (currently host-side).

Already handled, for the record: the step cap, the `input()` hang, and API errors masquerading
as success are all fixed in `rlgym/agents/gemini_cu.py` rather than inherited from upstream.

---

## 8. Rollout decisions ✅ DECIDED (Phase 2 Step 8) — ⬜ not yet implemented

Signed off by the user. Recorded as the contract for the wiring phase; **no code written**.

### 8.1 Auth — real admin credentials, used visually

The agent logs in as `daksh@deeptune.com` through the login form like a human would. No
extra user is seeded, so the restored env stays exactly as dumped.

Design around two consequences: the password enters the model's context and therefore
rollout transcripts (**scrub it at the logging layer**), and every seeded user is a
superuser so the agent runs with full admin. The per-rollout reset is what makes the latter
acceptable — anything it breaks is wiped by the next restore.

### 8.2 Reset — Postgres `TEMPLATE`

Restore the dump **once** into a pristine template database, then per rollout:

```sql
DROP DATABASE metabase_app;
CREATE DATABASE metabase_app TEMPLATE metabase_pristine;
```

Two things Postgres will enforce, to handle when building this:
- `CREATE DATABASE ... TEMPLATE` **fails if any session is connected to the template**.
- Metabase must be fully stopped, or it holds a connection to the database being dropped.

Build the template with the **in-container** `pg_restore` (§6.1). The ~30–60s Metabase JVM
restart still dominates reset cost — the template removes the `pg_restore` portion, not the
restart.

Still true and worth remembering: the H2 sample database lives in the **container layer**,
not a volume. A rollout that writes to the sample *data* (rather than Metabase metadata)
needs that file reset too — recreating the `mb-app` container does it.

### 8.3 Harness lifecycle — fresh browser per attempt

Strongest isolation; cookies, localStorage and the Metabase session all start clean, so no
rollout inherits hidden state from its predecessor. Needs **zero changes** to the repo's
`PlaywrightComputer` — it already launches in `__enter__` and closes in `__exit__`, so one
`with PlaywrightComputer(...)` block per attempt is exactly this. The ~1–3s launch cost is
negligible next to the Metabase restart.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker: command not found` | Homebrew bin not on PATH | `export PATH="/opt/homebrew/bin:$PATH"` |
| `Cannot connect to the Docker daemon` | Colima VM is down | `colima start` |
| `docker compose` → "not a docker command" | Compose plugin dir not registered | §2.3 |
| `psql`/`pg_restore` not found | `libpq` is keg-only | §2.4 |
| Metabase 502 / connection refused right after `up` | JVM still migrating | wait for `healthy`, §4.5 |
| `Metabase cannot use the plugins directory` | `MB_PLUGINS_DIR` points somewhere the non-root JVM can't write | leave it default, §4.6 |
| `role "postgres" does not exist` on restore | app DB user ≠ dump owner | keep `POSTGRES_USER=postgres` |
| Env var change appears ignored | compose reused the container | `up -d --force-recreate metabase` |

---

## 10. Full rebuild from scratch

```bash
brew install colima docker docker-compose
mkdir -p ~/.docker && printf '{"cliPluginsExtraDirs":["/opt/homebrew/lib/docker/cli-plugins"]}' > ~/.docker/config.json
colima start --cpu 4 --memory 8 --disk 60

cd /Users/gkumar/assessments/hackathon/metabase
docker compose up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done

# seed data
docker compose stop metabase
docker exec mb-postgres psql -U postgres -d postgres \
  -c "DROP DATABASE IF EXISTS metabase_app;" -c "CREATE DATABASE metabase_app OWNER postgres;"
docker exec mb-postgres pg_restore -U postgres -d metabase_app /resources/metabase_envdata.sql
docker compose up -d metabase
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done

# then open http://localhost:3000 and log in as daksh@deeptune.com
```

---

## 11. Shell gotchas hit while building this

- **zsh does not word-split unquoted expansions.** `set -- $spec` inside a loop passes the
  entire string as `$1` (bash would split it). Use `${=spec}` in zsh, `read -r a b c`, or
  write the values out explicitly. This failed silently — the loop ran with garbage args.
- Foreground `sleep` is unavailable in some tool contexts; `until ...; do sleep 3; done`
  inside a backgrounded command works.

---

## 12. computer-use-preview internals ✅ (read-only study, Phase 2 Step 5)

Reference clone at `agent/computer-use-preview/` (`--depth 1`, Apache-2.0). Written as a
read-only study before any code existed, and kept because it documents *why* our loop departs
from upstream's. §12.6 in particular is the list of defects `rlgym/agents/gemini_cu.py` exists
to avoid — we reuse upstream's `Computer` implementations and `handle_action()` mapping, and
replace its `agent_loop()`.

### 12.1 The action loop

`agent.py` → `BrowserAgent.agent_loop()` calls `run_one_iteration()` until it returns
`COMPLETE`. One iteration:

```
send full contents history + ComputerUse tool
   → candidate = response.candidates[0]
   → reasoning text (thoughts included) + zero or more FunctionCall parts
   → NO function calls  ==> task is done, return COMPLETE
   → for each call: handle_action() dispatches onto the Computer
        Computer returns EnvState(screenshot: PNG bytes, url: str)
   → append FunctionResponse{ response: {url}, parts: [inline PNG blob] }
   → prune screenshots older than the last 3 turns
   → CONTINUE
```

Context growth is bounded by `MAX_RECENT_TURN_WITH_SCREENSHOTS = 3` — older turns keep their
text but their images are set to `None`.

### 12.2 Action types — two sets, selected by model name

The schema is declared **server-side** by
`types.Tool(computer_use=types.ComputerUse(environment=ENVIRONMENT_BROWSER))`. The model
receives it from the API; there are no local function declarations for these. Individual
actions can be suppressed with `excluded_predefined_functions`, and extra custom functions
can be added alongside (the repo ships a `multiply_numbers` example).

**Modern set** — `gemini-3.5-flash` and newer, including the default `gemini-3.6-flash`:

| | | | |
|---|---|---|---|
| `click` | `double_click` | `triple_click` | `middle_click` |
| `right_click` | `mouse_down` | `mouse_up` | `move` |
| `type` | `drag_and_drop` | `wait` | `press_key` |
| `key_down` | `key_up` | `hotkey` | `take_screenshot` |
| `scroll` | `go_back` | `go_forward` | `navigate` |

**Legacy set** — `gemini-2.5-computer-use-preview-10-2025`, `gemini-3-flash-preview`,
`gemini-3.1-pro-preview` (`LEGACY_COMPUTER_USE_MODELS`):

`open_web_browser`, `click_at`, `hover_at`, `type_text_at`, `scroll_document`, `scroll_at`,
`wait_5_seconds`, `go_back`, `go_forward`, `search`, `navigate`, `key_combination`,
`drag_and_drop`

Which path runs is decided once in `__init__` via
`self._use_legacy_computer_use_function_call = model_name in LEGACY_COMPUTER_USE_MODELS`, and
`handle_action()` branches to `handle_legacy_action()`. Note the two sets differ in more than
naming — legacy `type_text_at` takes coordinates plus `clear_before_typing`, while modern
`type` types at the current focus and has no coordinates.

### 12.3 Coordinates are normalized 0–1000

```python
def denormalize_x(self, x): return int(x / 1000 * self._browser_computer.screen_size()[0])
def denormalize_y(self, y): return int(y / 1000 * self._browser_computer.screen_size()[1])
```

The model never sees real pixels, so the same policy transfers across viewport sizes.
`screen_size()` reads the **live** Playwright viewport, falling back to the constructor value.

### 12.4 What it expects as "the screen"

- **Raw PNG bytes**, viewport-only (`self._page.screenshot(type="png", full_page=False)`)
- Delivered as `types.FunctionResponsePart(inline_data=types.FunctionResponseBlob(
  mime_type="image/png", data=...))`, with the current URL in the response dict
- Default viewport **1440x900** (`PLAYWRIGHT_SCREEN_SIZE` in `main.py`)
- No resolution is enforced anywhere — changing the viewport is safe
- **It brings its own harness.** `computers/playwright/playwright.py` is a complete
  Playwright implementation; `computers/browserbase/` is a remote alternative. Both implement
  the abstract `Computer` interface in `computers/computer.py`. We do not need to write one.
- `current_state()` sleeps a hard `time.sleep(0.5)` after `wait_for_load_state()` because
  Playwright reporting "loaded" doesn't guarantee rendering finished.

### 12.5 Safety and refusal behaviour

Three distinct mechanisms, and the first one matters a lot for unattended rollouts:

1. **Per-action confirmation.** If a function call's args contain `safety_decision` with
   `decision == "require_confirmation"`, `_get_safety_confirmation()` prints the explanation
   and calls **`input()`** — blocking on a human at the terminal. Answering no returns
   `TERMINATE` and ends the loop; answering yes sets `safety_acknowledgement: "true"` on the
   function response. ⚠️ **In a batch/unattended rollout this hangs forever.** Must be
   handled before any automated run.
2. **Prompt-level block.** `prompt_feedback.block_reason == SAFETY` with no candidates
   raises `ValueError`.
3. **Malformed function calls.** `FinishReason.MALFORMED_FUNCTION_CALL` with no text and no
   calls just returns `CONTINUE`, retrying the turn.

### 12.6 Sharp edges found while reading (relevant to us)

| Issue | Detail |
|---|---|
| **`navigate()` forces https** | Any URL without a scheme gets `"https://" + url` prepended. `navigate("localhost:3000")` becomes `https://localhost:3000` and fails. **Always pass `http://localhost:3000`.** |
| **`PLAYWRIGHT_HEADLESS` is a truthiness trap** | `headless=bool(os.environ.get("PLAYWRIGHT_HEADLESS", False))` tests the *string*, so `PLAYWRIGHT_HEADLESS=false` yields `headless=True`. To run headed, leave it **unset or empty**. |
| **No max-iteration cap** | `agent_loop()` is `while status == "CONTINUE"` with no step limit. An RL rollout needs its own cap. |
| **API errors look like success** | `run_one_iteration()` wraps `get_model_response()` in `except Exception: return "COMPLETE"`. After the 5 internal retries are exhausted, a genuine API failure is indistinguishable from task completion. Any grading layer must not treat `COMPLETE` as "task succeeded". |
| **Single tab only** | `_handle_new_page` closes any popup and re-navigates the current page to its URL. |
| **`<select>` elements** | Known upstream issue: OS-rendered dropdowns don't appear in screenshots on some platforms. Metabase uses custom dropdowns in most places, but worth remembering if the agent stalls on a native picker. |
| **README vs code on browsers** | README says `playwright install chrome`; the code calls `chromium.launch()` with no `channel=`, so bundled **Chromium** is what actually runs. Install `chromium`. |
| **`.env.example` breaks the API-key path** | `agent.py` passes `project=`/`location=` to `genai.Client()` unconditionally, even with `USE_VERTEXAI=false`. Since `.env.example` ships `VERTEXAI_LOCATION=us-central1`, copying it verbatim and adding a key raises `ValueError: Project/location and API key are mutually exclusive in the client initializer.` **Both Vertex vars must be empty when using `GEMINI_API_KEY`.** See §7.2. |
