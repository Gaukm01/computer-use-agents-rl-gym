# Design & Setup — Metabase Computer-Use RL Environment

Short technical overview: what we're building, how the pieces fit, what's decided, and what
still needs your call. Read this one first.

- **This doc** — architecture, design decisions, condensed setup. ~10 min read.
- `INFRA_KNOWLEDGEBASE.md` — exhaustive reference. Every command, version, and gotcha.
  Go there when something breaks or you need to rebuild from scratch.
- `PROGRESS.md` — the running log of what was done, verified, and corrected.

---

## 1. What we're building

An **RL environment for testing computer-use agents**. The thesis: give a model a real,
non-trivial web app and see whether it can accomplish analyst-style tasks using nothing but
eyes and hands — screenshots in, mouse/keyboard out.

Three parts, built in order:

| Part | What it is | Status |
|---|---|---|
| **Gym** | Metabase + a realistic seeded environment (dashboards, questions, users) | ✅ done |
| **Agent** | Gemini computer-use driving a real browser against the gym | ✅ done — `problem10` passes end to end |
| **Rollout infra** | Task definitions, resets, grading, running many attempts | 🟡 one attempt + grading done; T×K runner and UI not built |

**The one hard constraint: visual only.** The agent sees screenshots and emits clicks and
keystrokes. No REST API calls, no SQL, no DOM inspection. If something looks easier via API,
that's a signal to flag it, not to take the shortcut — an API path would measure the wrong
thing entirely.

To be precise about where that line falls, since the harness does touch the DOM in two
places: **the agent** perceives the page only through screenshots. **The harness** reads the
DOM twice, never feeding either result to the model — a pre-flight check that we aren't
sitting on `/auth/login` (so a stale cookie is reported as infrastructure failure rather than
agent failure), and telemetry noting whether the SQL editor appeared (so pass rates can be
split by strategy). Grading likewise queries Postgres directly, because the scorer is not the
policy.

---

## 2. Architecture

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │  YOUR MAC (host)                                                     │
   │                                                                      │
   │   ┌─────────────────────┐         ┌──────────────────────────────┐   │
   │   │  agent process      │         │  Chromium (Playwright)       │   │
   │   │  python 3.12        │ drives  │  viewport 1440x900           │   │
   │   │                     ├────────►│  headed or headless          │   │
   │   │  BrowserAgent       │         │                              │   │
   │   │   loop:             │◄────────┤  returns PNG + current URL   │   │
   │   │   screenshot →      │  sees   └───────────────┬──────────────┘   │
   │   │   model → action    │                         │ HTTP             │
   │   └──────────┬──────────┘                         │ localhost:3000   │
   │              │                                    │                  │
   │              │ HTTPS                              │                  │
   │              │                    ┌───────────────▼──────────────┐   │
   │              │                    │  COLIMA VM → Docker          │   │
   │              │                    │                              │   │
   │              │                    │  ┌────────────────────────┐  │   │
   │              │                    │  │ mb-app  :3000          │  │   │
   │              │                    │  │ metabase v0.54.15      │  │   │
   │              │                    │  │  └ H2 sample DB        │  │   │
   │              │                    │  │    (the analytics data)│  │   │
   │              │                    │  └───────────┬────────────┘  │   │
   │              │                    │              │ JDBC          │   │
   │              │                    │  ┌───────────▼────────────┐  │   │
   │              │                    │  │ mb-postgres  :5433     │  │   │
   │              │                    │  │ postgres 16.9          │  │   │
   │              │                    │  │  └ metabase_app        │  │   │
   │              │                    │  │    (dashboards, users) │  │   │
   │              │                    │  └────────────────────────┘  │   │
   │              │                    └──────────────────────────────┘   │
   └──────────────┼───────────────────────────────────────────────────────┘
                  ▼
          ┌───────────────┐
          │  Gemini API   │   gemini-3.6-flash + ComputerUse tool
          │  (cloud)      │   returns reasoning + function calls
          └───────────────┘
```

**The loop, in one line:** screenshot → Gemini → function call (`click`, `type`, `scroll`…) →
Playwright executes it → new screenshot → repeat until the model stops calling functions.

### Two databases, easily confused

| | Postgres (`mb-postgres`) | H2 (inside `mb-app`) |
|---|---|---|
| Holds | Metabase's **own state** — dashboards, cards, users, collections | The **analytics data** the dashboards query |
| Comes from | `resources/metabase_envdata.sql` (our seed dump) | Bundled inside the Metabase image |
| Reset by | `pg_restore` / `TEMPLATE` | recreating the container |

The seed dump only contains Metabase's app state. It needed **no external data warehouse** —
the dashboards query the sample dataset that ships in the image.

---

## 3. Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Container runtime | **Colima** | No Docker Desktop licensing, CLI-only, no GUI |
| Metabase app DB | **Postgres, not H2** | The seed dump is a Postgres dump; H2 has nothing to restore into |
| Metabase version | **pinned `v0.54.15`** | Dump's schema is v0.54-era. A v0.55+ image would migrate it forward and permanently alter the seeded env |
| Restore method | **in-container `pg_restore`** | Host client is 18.4 vs 16.9 server → emits a PG17-only setting and logs a spurious error |
| Agent runtime | **Python 3.12** | Repo's CI is 3.10/3.11/3.x; system Python is 3.9.6 |
| Browser harness | **the repo's own Playwright impl** | `computer-use-preview` ships a complete one — nothing to build |
| Agent auth | **real admin creds, typed visually** | Simple; the per-rollout reset bounds the blast radius of admin rights |
| Reset strategy | **Postgres `TEMPLATE`** | Restore once, then `CREATE DATABASE … TEMPLATE pristine` per rollout |
| Browser lifecycle | **fresh browser per attempt** | Zero state leakage; needs no changes to the repo's harness |

---

## 3b. The rollout pipeline — decisions made while building it

Each layer is the atomic unit for the one above. **Nothing is rewritten between
milestones** — the orchestrator calls the same `execute_attempt()` that `run_one` calls, and
the API will call the orchestrator through `service.py`.

```
   web/ (React+TS) ──► server/app.py  thin routes, no logic
                    ┌───────────────────────▼──────────────────────┐
                    │ service.py    no web framework imported      │
                    │               submit / poll / cancel / read  │
                    └───────────────────────┬──────────────────────┘
                                            ▼
                    ┌──────────────────────────────────────────────┐
                    │ orchestrator.py   T tasks × K attempts       │
                    │   thread pool, attempt-major, resumable      │
                    │   pass@k over VALID attempts only            │
                    └───────────────────────┬──────────────────────┘
                                            │  (one call per attempt)
tasks.json ─► Task ─────────────────────────▼
                        execute_attempt()            ONE attempt, own subprocess
                                │                    ├─ GymComputer  pre-auth, video, lock
                                │                    └─ Agent        Gemini + submit_answer
                                ▼
                        AttemptArtifact              status, steps, screenshots, tokens
                                │
                                ▼
                        grade() ─► EvaluationResult  verdict + reward + diff (versioned)
```

Every layer calls the one below it and nothing re-implements it: `scripts/run_one.py`,
the job orchestrator, and the future API all bottom out in the same `execute_attempt()`.

### Reward derives from status, and `ERRORED` is not zero

```
COMPLETED_CORRECT     reward 1
COMPLETED_INCORRECT   reward 0     includes "stopped without submitting"
MAX_STEPS_EXCEEDED    reward 0     agent failure: had budget, didn't finish
TIMED_OUT             reward 0
ERRORED               reward null  outcome never observed -- excluded from pass@k
```

The asymmetry is the whole point. `ERRORED` means a Gemini 5xx, a browser crash, or an
expired session — *we* failed, not the agent. Scoring it 0 would make reward correlate with
infrastructure flakiness and quietly bias any dataset built from these artifacts. Verified:
an invalid API key yields `ERRORED / reward null / not_graded`, carrying the real 401.

Per your call, `ERRORED` attempts are **not retried** — transient 429s are already absorbed
by the SDK's internal backoff below the attempt boundary, and k attempts provide the
redundancy. The runner will report `passed / failed / errored` per task and flag any task
where `valid < k` so a pass@3 computed on n=1 can't look clean.

### The agent submits through a tool, not through prose

`submit_answer(answer_json)` is registered alongside the computer-use tools; calling it ends
the episode. Every task wants a structured dict, and upstream's episode ends when the model
merely stops emitting calls — which would mean grading parsed prose. This also makes
termination explicit rather than inferred. It doesn't weaken visual-only: the tool is the
agent's mouth, not its eyes.

### One subprocess per attempt — required, not stylistic

Playwright's **sync API is not thread-safe** (multiple `sync_playwright()` instances across
threads fail), so threads can't provide parallelism. A subprocess also makes a wall clock
enforceable against a genuinely hung process, and contains a crashing browser to one
attempt. The agent additionally honours a *soft* deadline so it can exit cleanly and flush
its video; the SIGKILL is only a backstop. The artifact is rewritten after every step, so a
killed attempt keeps its partial trajectory.

### The agent is told its remaining budget

Added because of an observed failure, not invented. On the first full `problem10` run the
agent had the correct value (`3.15`) on screen at **step 12**, then spent the remaining 28
steps re-running queries trying to decide whether Metabase's display was rounded or exact,
and hit the cap without ever submitting. It had no way to see its budget draining.
Observations now carry `steps_remaining`, plus an explicit submit instruction in the last 8
steps. The same task then passed. Reporting remaining budget is standard in agent harnesses —
without it the model is asked to manage a resource it cannot observe.

### Environment boundary vs action restrictions — two different things

- **Domain lock** (invariant): top-level navigation is confined to the Metabase origin;
  sub-resource loads from other hosts are untouched. `search` **stays in the tool set** and
  is refused, rather than being repointed at the gym — an observable boundary beats a silent
  redirect that looks like a search returning nothing.
- **`prohibited_actions`** (per-run config, **empty by default**): nothing is blocked today.
  The agent may use the SQL editor or the query builder, its choice; which one it used is
  *logged* so pass rates can be split by strategy later.

### Pre-authentication

Attempts start authenticated by injecting a saved Playwright `storage_state`. The password
never enters model context or a transcript, no attempt burns 3–5 steps on a login form, and
a login failure can't be confounded with an analysis failure. A stale cookie is caught by a
pre-flight check and reported as `ERRORED` — infrastructure, not agent.

⚠️ Metabase sessions live in `core_session` in the app DB, so **any DB restore invalidates
the cookie**. Harmless now (read-only tasks, no resets); in write-mode it must be re-captured
after each restore. Don't bake a session row into the pristine template — Metabase ages
sessions out (~14 days) and a baked one rots silently.

### Jobs, and what pass@k actually counts

**Terminology**, kept precise because the words are overloaded elsewhere:

| Term | Meaning |
|---|---|
| **Task** | A problem definition from `tasks.json`. Static, never runs. |
| **Attempt** = **rollout** | One episode: one agent run against one task, once. The atomic unit. |
| **Job** | A batch: T tasks × K attempts = T·K independent attempts. |

`K` defaults to **1** so an exploratory job is cheap; pass 3+ for pass@k.

**ERRORED attempts leave the denominator.** This is the arithmetic that keeps a job's
headline number honest:

```
1 pass + 1 fail + 1 errored   →   pass_rate = 50%,  NOT 33%
```

Per task we report `pass_at_k` (did *any* valid attempt succeed — pass@k proper),
`pass_rate` (passed ÷ valid), and `low_confidence` when errors left fewer valid attempts
than requested — so a "pass@3" actually computed on n=1 can't read as clean. At job level,
`pass_at_k_rate` averages over **graded tasks only**: a task whose every attempt errored is
excluded rather than counted as a failing task.

### Threads in the orchestrator, subprocesses underneath

`execute_attempt()` already forks a subprocess and blocks waiting on it, so the pool only
supervises. Threads are therefore correct despite the GIL — they're parked in `Popen.wait()`,
not computing — and Playwright never runs in the orchestrator process, which is the thing
that would have made threads unusable.

Attempts are ordered **attempt-major**: every task's attempt 0 before any task's attempt 1.
A half-finished job then has one sample of every task rather than every sample of half the
tasks, which is far more useful to look at while it runs.

Jobs stream their state to `runs/<job_id>/job.json` after every attempt, so a UI follows
progress by polling one file — no websocket plumbing — and an interrupted job leaves a
resumable record. Resume skips any attempt already in a terminal state, including ERRORED
(attempt-level retry is deliberately off).

### Grader comparison rules

Numbers compare with tolerance after stripping `,` `$` `%` (so `"101,203.30" == 101203.3`
and `5 == 5.0`). Strings are whitespace-collapsed and casefolded. Key sets must match
exactly. Lists compare as **multisets**, since `problem9` is a set of names where order
carries no meaning.

The subtle one is **pairing**: `problem1` and `problem2` return parallel arrays, so
equal-length lists are zipped into tuples before comparison. Comparing them independently
would accept an answer with all the right titles and all the right ratings paired *wrongly*.
Zipping also preserves `problem2`'s ranking implicitly, since each count travels with its
title.

Near-misses (structurally right, just outside tolerance — e.g. `"Q4 2025"` for `"Q4"`) are
flagged for review rather than trusted as clean agent failures.

---

## 4. Open questions — where your input actually matters

These are unresolved and shape the build. Nothing here is decided.

**1. What is a task, and how do we grade it?** The biggest open question by far. A task needs
a goal the agent can pursue visually *and* a success check we can run afterwards. The natural
check is SQL against Postgres after the rollout ("does a card named X now exist in collection
Y?") — that's grading infrastructure, not agent behaviour, so it doesn't violate visual-only.
Open: do we grade final state only, or also the path taken? `resources/tasks.json` exists but
is untouched and unscoped.

**2. How do we know a rollout actually failed?** The repo's loop returns `COMPLETE` both when
the model finishes *and* when the API errors out after retries. Any grading layer must treat
"loop ended" as meaningless on its own and judge from observed state.

**3. Step cap and timeout.** The upstream loop is `while CONTINUE` with no limit. What's the
budget — 20 steps? 50? Wall-clock too? This directly affects cost per rollout.

**4. Unattended safety confirmations.** Some actions come back flagged `require_confirmation`,
and the repo blocks on `input()`. For batch runs we must auto-approve, auto-deny, or abort
the rollout. Auto-approving is a real (if small) policy decision — your call.

**5. What the model gets as observation.** Currently screenshot + URL, with only the last 3
turns' images retained. Do we keep that, or give it more/less history? Affects both cost and
capability.

**6. Rollout parallelism.** One gym on port 3000 means one rollout at a time. Running N in
parallel needs N Metabase+Postgres pairs on different ports — decide before writing the
runner, since it changes its shape.

---

## 5. Setup from scratch

```bash
# 1. Host tools
brew install colima docker docker-compose
mkdir -p ~/.docker
printf '{"cliPluginsExtraDirs":["/opt/homebrew/lib/docker/cli-plugins"]}' > ~/.docker/config.json

# 2. Start the VM (does NOT survive reboot — this is the #1 daily gotcha)
colima start --cpu 4 --memory 8 --disk 60

# 3. Start the gym
cd hackathon/metabase && docker compose up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done

# 4. Seed it
docker compose stop metabase
docker exec mb-postgres psql -U postgres -d postgres \
  -c "DROP DATABASE IF EXISTS metabase_app;" -c "CREATE DATABASE metabase_app OWNER postgres;"
docker exec mb-postgres pg_restore -U postgres -d metabase_app /resources/metabase_envdata.sql
docker compose up -d metabase

# 5. Agent environment
cd ../agent
/opt/homebrew/bin/python3.12 -m venv .venv
./.venv/bin/pip install -r computer-use-preview/requirements.txt
./.venv/bin/playwright install chromium

# 6. Secrets — put GEMINI_API_KEY in hackathon/.env
#    Leave VERTEXAI_PROJECT and VERTEXAI_LOCATION EMPTY (see traps below)
```

Then open http://localhost:3000 → log in as `daksh@deeptune.com` / `Daksh@123`.

---

## 6. Running the parts

| Goal | Command |
|---|---|
| **Start the platform** | `agent/.venv/bin/uvicorn server.app:app --port 8000` → http://localhost:8000 |
| Rebuild the UI | `export PATH="$HOME/.nvm/versions/node/v18.20.8/bin:$PATH" && cd web && npm run build` |
| Run a job from the CLI | `agent/.venv/bin/python -m rlgym.scripts.run_job --tasks problem10 --attempts 3` |
| Is the gym up? | `docker compose ps` · `curl -s localhost:3000/api/health` |
| Mint the auth cookie (once) | `agent/.venv/bin/python -m rlgym.capture_auth` |
| **Run one attempt** | `agent/.venv/bin/python -m rlgym.run_one --task problem10` |
| Watch it work | `... -m rlgym.run_one --task problem1 --headed` |
| Fast plumbing check (1 API call) | `... -m rlgym.run_one --task problem10 --max-steps 1` |
| Run the tests | `agent/.venv/bin/python -m pytest tests/ -q` |
| Is my API key good? | `cd agent && ./.venv/bin/python verify_gemini.py` |
| Inspect the seeded data | `psql -h localhost -p 5433 -U postgres -d metabase_app` |

Exit code is `1` only for `ERRORED`. A wrong answer is a valid outcome and must not look like
a broken pipeline to a caller or CI step.

The Phase 2 scripts (`verify_gemini.py`, `verify_harness.py`) still work and remain useful for
isolating "is the key live?" from "is the gym reachable?" when something breaks.

---

## 7. Traps that will bite you

1. **`colima start` before anything.** The VM doesn't survive a reboot. `docker ps` failing
   with "cannot connect to the daemon" always means this.
2. **`VERTEXAI_LOCATION` must be empty.** The repo passes it to `genai.Client()` even when
   Vertex is off, and the SDK rejects it alongside an API key. Upstream's own `.env.example`
   ships a value — copying it verbatim breaks immediately.
3. **`PLAYWRIGHT_HEADLESS=false` means headless=True.** It's a truthiness check on the
   string. Leave it empty for a visible browser.
4. **URLs need `http://`.** `navigate()` prepends `https://` to anything without a scheme, so
   bare `localhost:3000` fails.
5. **`psql`/`pg_restore` aren't on PATH.** Homebrew's `libpq` is keg-only:
   `export PATH="/opt/homebrew/opt/libpq/bin:$PATH"`.
6. **Metabase takes 30–60s to boot.** Wait for the healthcheck, don't assume.

---

## 8. Build status

```
✅ Gym          Metabase v0.54.15 + Postgres 16.9, seeded and verified
                   92 tables · 12 users · 6 dashboards · 86 cards
✅ Python env   3.12 venv, google-genai 2.8.0, playwright 1.55.0, chromium 140
✅ API key      live, all 5 computer-use models reachable
✅ Harness      renders the gym at 1440x900 PNG (screenshot reviewed)
✅ Pre-auth     storage_state injection; attempts start logged in
✅ Agent loop   our own -- step cap, no input() hang, API errors surfaced
✅ submit_answer  explicit answer channel, terminates the episode
✅ Executor     subprocess per attempt, soft deadline + SIGKILL backstop
✅ Grader       pairing-aware, versioned, 44 tests passing
✅ End-to-end   problem10 -> COMPLETED_CORRECT, reward 1, fully visual
✅ Orchestrator T×K in parallel, attempt-major, resumable, pass@k
✅ Service      framework-agnostic seam the API will sit on
✅ CLIs         scripts/run_one.py (one attempt) · scripts/run_job.py (a job)
✅ HTTP API     FastAPI over service.py; /docs for the interactive spec
✅ Frontend     React + TS eval console -- upload tasks, set K, view transcripts
⬜ Reset        TEMPLATE approach decided, not implemented (unneeded: read-only tasks)
⬜ M3           productionization design not written
```

**Milestone 2's three UI requirements**, all working: submit a job from a tasks file,
specify attempts per problem, and visually review each attempt's transcript and grade.

**Measured job throughput:** 6 real attempts at concurrency 3 finished in 16s wall clock
(sequential would have been ~48s), with three attempts starting simultaneously and the next
three starting as slots freed. Resume of a completed job re-ran nothing in 0.16s.

**Measured cost per attempt** (problem10, 39 steps): ~518k tokens, ~4.5 min wall clock.
Prompt tokens dominate at ~13k/step — three 1440×900 screenshots in context. That number is
the main lever if cost becomes a constraint: fewer retained screenshots, or a smaller
viewport.
