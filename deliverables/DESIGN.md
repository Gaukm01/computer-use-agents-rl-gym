# Design — What We Built

A computer-use RL environment for Metabase: the gym, the agent, and the infrastructure that
turns rollouts into reward. Reference notes for a code walkthrough.

Productionisation plan: [`PRODUCTION.md`](./PRODUCTION.md)

---

# 1. Features built

- **Ephemeral gym per rollout** — one `docker compose` project holding Metabase + Postgres,
  seeded and destroyed per rollout. Nothing shared, so nothing to reset.
- **Runtime seeding** — `pg_restore` of `metabase_envdata.sql` into a fresh Postgres *before*
  Metabase is started.
- **Visual-only agent** — Gemini computer-use driving real Chromium via Playwright. Screenshots
  in, mouse/keyboard out; no API, SQL, or DOM access for the model.
- **Answer channel** — a `submit_answer` tool ends the episode. No format hint given; choosing
  the answer's shape is part of the task.
- **Domain lock** — off-origin top-level navigation is refused and the refusal is handed back to
  the model as an observation, not removed from its action space.
- **Deterministic grader** — pure function of `(artifact, task)`, versioned, so stored
  trajectories can be re-scored without re-running anything.
- **Reward from status** — `1` / `0` / `null`. Infrastructure failure scores `null` and leaves
  the pass@k denominator.
- **Parallel rollouts** — thread pool over subprocesses over containers, bounded by a port queue
  (3 concurrent, memory-bound).
- **Job orchestration** — T tasks × K attempts, task-major, resumable, cancellable, with pass@k
  and mean reward computed per task and per job.
- **Live artifacts** — `artifact.json` is rewritten on every phase change and every step, so a
  killed rollout keeps its trajectory and the UI can follow along by polling a file.
- **Web console** — upload a `tasks.json`, pick tasks, set attempts and concurrency, watch phases
  live, open any finished rollout.
- **Transcript viewer** — per step: screenshot, model reasoning, action + args. Plus video, the
  exact first prompt, and a grade panel with expected vs actual and the diff.
- **Host-disk reclaim** — `fstrim` after every teardown, plus a free-space floor that ends a job
  cleanly instead of crashing it.
- **Health gate** — Docker, both images, the dump, and credentials checked before a run.
- *Also:* tasks.json upload + validation, per-rollout `spec.json`, `worker.log` capture,
  path-traversal guard on media, legacy-status translation, server-served agent defaults,
  hash routing, 109 tests.

---

# 2. Not built / out of scope

Five things we would do next. Each is scoped, not speculative.

**1. Pristine-copy validation as an advisory signal, not a hard gate.**
A seeded row-count mismatch currently raises during provisioning and the rollout becomes
`NOT_OBSERVED` — strict, and it can cost a rollout for drift in a table we only assumed was
stable. *Way to do it:* keep the query, drop the raise, record the counts plus a boolean on the
artifact — drift becomes a trust score on the result rather than a failure of it.

**2. Job run setup — a queue instead of a warning.**
Only one job runs at a time: a job start sweeps orphaned containers and would destroy a
concurrent job's gyms, so the console confirms first. There is also no way to re-run just the
rollouts whose outcome was never observed. *Way to do it:* one worker thread draining a
`Queue[JobSpec]` — `ports.py` is already this pattern and `JobStatus.QUEUED` sits unused in the
enum; rollout-level retrigger then becomes re-enqueueing the failed slots.

**3. Ground-truth correctness check, with a human in the loop.**
`problem2` fails every attempt because its provided answer is not reproducible in this database
— found by hand with SQL, outside the platform, so nothing in the system distinguishes *"the
agent was wrong"* from *"the task is wrong"*. *Way to do it:* have tasks carry the query that
defines their answer, recompute it against a fresh gym on a schedule, and flag disagreements for
review — the same surface collects human verdicts on near-misses.

**4. Tool clock vs wall clock.**
`duration_s` is wall clock: model latency, network, browser actions and page loads in one
number. Useful for capacity planning, useless for reasoning about the agent. *Way to do it:*
both halves are already recorded per step as `latency_ms` — split model time from action time,
report them separately, and apply the timeout budget to agent time rather than infrastructure.

**5. Concurrency is capped at 3.**
Bound by VM memory, not by anything clever: each rollout holds its own Metabase (~1.08 GiB) in a
7.74 GiB VM, and 33.6 s of every rollout goes on booting a stack that is then thrown away.
*Way to do it:* a pre-warmed pool moves the boot off the critical path; beyond that concurrency
is a machine-count problem, which is where [`PRODUCTION.md`](./PRODUCTION.md) picks it up.

*Write tasks, distributed execution and durable state are deliberately deferred to
[`PRODUCTION.md`](./PRODUCTION.md) rather than half-built here.*

---

# 3. How it works

## 3.1 Job submitted → queued and scheduled

### 3.1.1 Validation and the job document

`service.submit_job()` → `rlgym/service.py:287`

- Rejects unknown task ids, `attempts < 1`, `concurrency < 1` *before* any work starts.
- `orchestrator.build_spec()` freezes the policy into a `JobSpec` — model, temperature, top_p,
  top_k, budget reminders, limits. The job page later reads config from this, not from the form.
- `_start_background()` runs the job on a daemon thread and joins 1 s, so the caller gets real
  state instead of a placeholder.

`run_job()` → `rlgym/orchestrator.py:263` writes a row for **all T×K rollouts immediately**, so
the console renders the full grid from second one. Placeholder rows carry no `attempt_id`, which
is what marks them un-openable.

### 3.1.2 Orphan sweep

`gym_stack.sweep()` removes every container labelled `rlgym.ephemeral` left by a Ctrl-C or a
killed API process, then **recounts** — an earlier version returned the number *found* and
reported "swept 6" having removed none.

### 3.1.3 Task-major queueing

```python
queue = [(task, index) for task in selected for index in range(K)]
```

A task's K rollouts are all *submitted* before the next task's, so a half-finished job has
complete, usable results for some tasks rather than one sample of everything. Submission order
only — there is no barrier, so a task's last rollout overlaps the next task's first.

### 3.1.4 Threading model

```python
workers = max(1, min(spec.concurrency, ports.MAX_GYMS))
with ThreadPoolExecutor(max_workers=workers) as pool: ...
```

Threads are correct here **because they only supervise** — each parks in `Popen.wait()` and
Playwright never runs in this process. Capped at `MAX_GYMS` because an extra thread would only
block inside `ports.claim()`.

### 3.1.5 Ports are the concurrency semaphore

`rlgym/ports.py` — the whole allocator is a `queue.Queue` pre-filled with 3001–3003.

- `claim()` blocks on `get()` when all three are taken. No pool manager, slot table, or leases
  anywhere in the system.
- The `finally` returns the port unconditionally, so a failed provision does not leak capacity.
- `MAX_GYMS = 3` is measured: 4 vCPU / 7.74 GiB VM, ~1.08 GiB per Metabase. **Memory is the
  ceiling, not API quota.** Port 3000 is left free for a manual dev gym.

### 3.1.6 Resume and the disk floor

- `load_completed_attempts()` skips any slot already terminal, so resuming costs only lost work.
- `_host_free_gib() < 8.0` → `cancel.set()`, checked per rollout. Turns a crash at rollout 25
  into a clean partial result with a stated reason.

### 3.1.7 Tracking rollouts via `artifact.json`

Two writers, and the split is the whole progress mechanism:

| Path | Source | When |
|---|---|---|
| `apply()` | the artifact the executor **returned** | rollout finished |
| `refresh()` | `artifact.json` **on disk**, every 2 s | rollout in flight |

A watcher thread polls only in-flight rollouts (`skip=settled`), so a live poll can never
overwrite a finished result, and no callback has to cross the subprocess boundary. `apply()`
uses the value in hand so a successful rollout cannot vanish over an I/O error.

---

## 3.2 One rollout, phase by phase

`execute_attempt()` → `rlgym/executor.py:334`. Phases: `QUEUED → PROVISIONING →
AUTHENTICATING → CONNECTING → RUNNING → GRADING → DONE`.

### 3.2.1 Subprocess isolation

```python
proc = subprocess.Popen([sys.executable, "-m", "rlgym.executor", "--worker", spec_path],
                        start_new_session=True)
proc.wait(timeout=limits.timeout_s + SHUTDOWN_GRACE_S)   # 45 s grace
```

Three independent reasons, any one of which forces it: Playwright's sync API is not
thread-safe; a wall clock must be enforceable by SIGKILL; a crashed browser should cost one
rollout. `start_new_session=True` gives its own process group, so `os.killpg()` takes the
browser and driver down with it.

**Provisioning and teardown live in the parent**, wrapping the `Popen` — SIGKILL runs no
`finally`, so teardown inside the worker would leak a container and a port on exactly the path
that most needs cleanup. It also keeps `timeout_s` a pure *agent* budget.

### 3.2.2 Pristine Metabase — containers and restore

`ephemeral_gym()` → `rlgym/gym_stack.py:417`

Project name is `rlgym-<attempt_id>`, sanitised — task ids come from an uploaded file and are
not guaranteed to be legal compose names.

```
1. docker compose up -d postgres        ← Postgres ONLY
2. wait for pg_isready (docker inspect health)
3. pg_restore /resources/metabase_envdata.sql   ← exit code checked, not assumed
4. docker compose up -d metabase        ← only now
5. poll /api/health until 200
6. assert seeded row counts
```

**Step 1 is Postgres alone on purpose.** A plain `compose up -d` starts Metabase the instant
Postgres reports healthy — `depends_on: service_healthy` — and Liquibase then builds a schema
into the empty database that collides with the restore. Ordering removes the race structurally
rather than by timing.

Compose details worth pointing at: `MB_HOST_PORT` is required with **no default** (a silent
fallback to 3000 would collide the moment two stacks run); no `container_name` (names are
globally unique per daemon); no restart policy (an ephemeral stack must not resurrect);
`/plugins` deliberately **not** volume-mounted, so each container extracts a fresh H2 sample
database from the image — analytics isolation for free.

Step 6 records the counts on the artifact: a partially restored database is wrong ground truth
presented as a valid run.

### 3.2.3 Auth

`mint_session()` → `rlgym/gym_stack.py:172`

- `POST /api/session` over HTTP, ~50 ms. **Deliberately not Playwright** — its sync API cannot
  be driven from pool threads, the same constraint that makes rollouts subprocesses.
- `write_storage_state()` writes the `metabase.SESSION` cookie with `domain: "localhost"`;
  cookies ignore port, so one shape works for every stack.
- `service.check_auth()` then calls `GET /api/user/current` — it proves the cookie **works**,
  not that a file exists. A stale session is infrastructure failure, not agent failure.

### 3.2.4 Agent hookup

#### Browser — `GymComputer.__enter__` → `rlgym/computer.py:101`

- Chromium launched headless with `--disable-file-system`, `--disable-extensions`,
  `--disable-dev-shm-usage`; viewport 1440×900.
- `storage_state` injected at context creation, so the rollout opens already authenticated.
- `record_video_dir` set; Playwright only finalises the file when the **context** closes, so
  `__exit__` reads `page.video` first, then closes context, then browser.
- `context.route(is_off_origin, guard)` — the predicate only matches off-origin URLs, so all
  Metabase traffic is never intercepted and pays no latency. Only main-frame navigation is
  aborted; sub-resources pass.
- Pre-flight: `wait_for_ready()` (networkidle) then `is_authenticated()` — if the URL is
  `/auth/login`, fail as `NOT_OBSERVED` rather than letting the agent burn its budget on a
  login form.

#### Prompt and tools

`build_prompt()` → `rlgym/executor.py:157`

```
TASK_PREAMBLE   "The app is already loaded in the browser and authenticated…"
+ task.task
+ SUBMIT_INSTRUCTION   (appended by the agent — it describes that agent's tooling)
```

The preamble states environment facts only: it does not name the product, say where anything
is, or describe the answer's shape.

Tools: `ComputerUse(environment=ENVIRONMENT_BROWSER, excluded_predefined_functions=[])` plus a
`submit_answer` declaration. Nothing is excluded — the domain lock is an environment invariant,
not a trimmed action space. `thinking_config(include_thoughts=True)` so reasoning is recorded.

#### The loop — `rlgym/agents/gemini_cu.py:168`

```
while len(steps) < max_steps:
    check deadline → TIMEOUT
    get_model_response()          # 5 retries w/ backoff upstream; raises, we classify
    extract_function_calls()
    for call:
        submit_answer? → parse, record, return SUBMITTED
        handle_action(call)       # upstream's mapping, reused verbatim
        screenshot → StepRecord → on_step() → write_artifact()
        FunctionResponse(inline PNG + url [+ reminder near cap] [+ error])
    _prune_old_screenshots()      # keep images for the last 3 turns only
```

- We subclass `BrowserAgent` but **do not call `super().__init__()`** — upstream builds its
  client with `project`/`location` unconditionally, which google-genai rejects alongside an API
  key.
- **Malformed calls are the agent's mistake.** `KeyError`/`TypeError`/… from `handle_action` are
  caught, the raw exception is returned as an observation, and the episode continues. It costs a
  step. Ending the rollout instead would convert a real agent failure into `NOT_OBSERVED` and
  delete it from the pass@k denominator — that happened to three rollouts in one job.
- **Budget reminders fire only near the cap.** Within `SUBMIT_REMINDER_THRESHOLD` actions of
  the limit, the observation carries a line telling the agent to finish, submit, and mark
  anything undetermined as unknown rather than guess. Nothing is attached the rest of the time,
  so the agent is not fed a countdown it did not need. `budget_reminders` is recorded per job,
  because a run with it off measures something different.
- **Safety confirmations are auto-acknowledged** and counted; upstream calls `input()` here,
  which would hang a batch run.
- Screenshot pruning matters: without it context grows by a full-page PNG per step and cost
  climbs quadratically.

**Termination:** `SUBMITTED` · `NO_FUNCTION_CALL` (model stopped acting) · `MAX_STEPS` ·
`TIMEOUT` · `API_ERROR` · `HARNESS_ERROR` · `SAFETY_BLOCKED`.

### 3.2.5 Grading layer

`grade_attempt()` → `rlgym/executor.py:510` · `grader.grade()` → `rlgym/grader.py:277`

Status gate first — only `ANSWERED`/`CORRECT` are compared; everything else takes its reward
from status. Only the grader may promote to `COMPLETED_CORRECT`; the reward function lives in
one place.

**Leniency — confined to how the answer arrives:**

- Numeric strings → float, stripping `,` `$` `%`. Only a string that is *entirely* a number
  converts, so `"Q4"` and product titles stay text.
- `math.isclose(rel_tol=1e-6, abs_tol=1e-2)` — `problem4` and `problem10` pass only because of
  this; the provided answers are rounded display values.
- Keys casefolded and whitespace-collapsed.
- `{"answer": X}` envelope unwrapped as a **fallback**, not unconditionally — a task whose real
  answer has one `answer` key would otherwise be unmatchable.

**Match handling:**

- Dicts: key sets must match exactly; missing/extra keys are reported by name.
- Lists: multiset comparison (greedy, because float tolerance is not hash-equivalent), so
  `problem9`'s name set passes in any order.
- **Parallel arrays — the one that catches a cheat.** Two or more equal-length list values under
  the same object are zipped into tuples before comparison. Otherwise an answer with every right
  title and every right rating, **paired wrongly**, would pass.
- `near_miss` flags structurally-right-but-outside-tolerance (within 5%, or substring for text)
  so a human looks instead of trusting it as a clean failure.

**Cleanup / containment:** a grader exception is caught and recorded as `not_graded` rather than
propagating — it runs inside a pool future, so one malformed `task.answer` would otherwise fail
a 30-rollout job. `EVALUATOR_VERSION` is stamped on every verdict, and `evaluation.json` is
written separately from the artifact so re-grading never mutates the trajectory.

### 3.2.6 Teardown, kill, and disk reclaim

Runs in the parent's `finally`, so it executes on every path including SIGKILL.

**Teardown escalates, then recounts:**

```
compose down -v --remove-orphans
  → down again                     (the daemon can be transiently busy under 3 stacks)
  → docker rm -f  by compose-project label
  → docker volume rm <project>_*   (docker rm does NOT remove named volumes)
  → RECOUNT containers and volumes
```

*The earlier miss:* the first version fired `down -v` and **ignored the exit code**. When the
host disk filled, `down` failed, six containers survived, nothing noticed — and since the port
returns to the pool regardless, the next rollout on that port died with "port already
allocated". One bad rollout poisoned every later one. Failure is now recorded as
`teardown_error` and never raised: the agent's result must survive an operational problem.

**Then `fstrim`** — `reclaim_host_disk()` → `rlgym/gym_stack.py:314`

The VM's data disk is ext4 mounted **without `discard`**, so `docker rm` frees blocks inside the
guest and nothing tells the host's sparse image. Growth was ~1 GiB per rollout, monotonic; two
30-rollout jobs died of it while the guest filesystem sat at 4% used. `fstrim` after teardown
took host growth to **0.8 MB per rollout**. Each artifact records `host_image_gib` and
`guest_used_gib` as a **pair** — a single number is what made this invisible for a whole job.

Serialised behind a lock (three lanes would duplicate the same free-block walk) and a no-op off
colima. Never raises; failure lands on `trim_error`.

**Then the port returns** via `ports.claim()`'s own `finally`.

---

## 3.3 What the console reads

- Job page polls `GET /api/jobs/{id}` every 2 s. `read_job()` recomputes derived numbers from
  the rollout rows, so a `job.json` written before a counter existed still reports it correctly.
- Transcript loads `artifact.json` + `evaluation.json`; screenshots and video stream through a
  media route that resolves paths inside the rollout directory only.
- `isGraded = terminal && phase === "DONE"` — the executor writes `ANSWERED` before grading, so
  gating on status alone would flash "Failed" on a rollout about to pass.
- No websockets. Polling a file is the entire live-progress mechanism.

---

# 4. Validation

Two different questions, and they need different evidence: **is the platform correct**, and
**how did the agent score**. A pass rate only answers the second.

## 4.1 Is the platform correct?

Not one successful run — every check below is recorded *per rollout* and holds across **65
rollouts in two independent jobs**. The gym, orchestration, executor and grader are unchanged
since those runs, so these results describe the current platform.

| Check | Evidence | Result |
|---|---|---|
| Every rollout starts from identical state | `pristine_counts` on each artifact | **65/65** identical: `{recent_views: 73, report_card: 86}` |
| Isolation holds; nothing leaks | `teardown_error`, `trim_error` per rollout | **0 and 0** |
| No port collision under concurrency | `gym_url` per rollout | 3001 ×21 · 3002 ×22 · 3003 ×22 |
| Parallelism is real, not nominal | wall clock vs serial estimate | **2.9×** on 3 lanes — *in both jobs independently* |
| The grader discriminates both ways | verdicts + diffs | 52 pass / 13 fail, **4 distinct** failure reasons |
| The gym reproduces the provided ground truth | independent SQL check | **9 of 10** answers reproduce exactly |
| Reward survives infrastructure failure | the two disk-exhaustion jobs | 25/30 graded and valid; 5 correctly `null` |

Two of those deserve a sentence:

- **The grader is not rubber-stamping.** `problem1`/`3`/`4` at 10/10 are the positive control,
  `problem2` at 0/10 for a diagnosable reason is the negative control, and `problem5` at 8/10
  shows the harness captures genuine sampling variance rather than being deterministic by
  accident. Failures come back with legible diffs, not a bare `false`.
- **2.9× is close to the 3.0× ceiling**, and it landed twice on different job shapes — so the
  bound really is the port queue, not contention we failed to notice.

## 4.2 What the agent scored

Pass rates measure the agent, not the platform, and they move with the prompt. The task preamble
and the near-cap reminder have been reworded since these runs, so treat the numbers as a
snapshot of that policy rather than a current benchmark.

**5 tasks × 10, concurrency 3 — 50 rollouts, 91 min, 21.8 M tokens**

| Task | pass@10 | Passed | Avg time | Avg steps |
|---|---|---|---|---|
| problem1 | yes | 10/10 | 241 s | 28 |
| problem3 | yes | 10/10 | 241 s | 31 |
| problem4 | yes | 10/10 | 215 s | 25 |
| problem5 | yes | 8/10 | 295 s | 34 |
| problem2 | **no** | **0/10** | 436 s | 49 |

pass@10 **80%** · mean reward **0.76** · **0 not-observed**

**5 tasks × 3 — 15 rollouts, 27 min, 7.1 M tokens.** pass@3 **100%** of tasks · mean reward
**0.93** · **0 not-observed.** This is the job committed under `runs/`, browsable without an
API key.

**Cost anatomy:** ~450 k tokens per rollout at prompt:output ≈ **380:1** — three 1440×900 PNGs
retained per turn, ~13 k prompt tokens per step. Entirely a screenshot problem.

## 4.3 The one negative result worth stating

**`problem2` cannot pass — the task, not the agent.** Its expected counts (996 / 700 / 664)
exceed any product's order count in this database; the busiest has 174. Three readings of the
prompt were checked with SQL and none reproduces it, and none of the three named products even
carries a rating of 5. That 9 of the other 10 answers reproduce exactly is what makes this a
task-level discrepancy rather than a grader bug. Graded against the provided answer, unchanged.

---

# 5. Known limitations

- **In-memory job registry** — restarting the API abandons in-flight jobs. Artifacts survive and
  `--resume` recovers them, but the abandoned job keeps reporting `RUNNING`.
- **The timeout path has never fired**, and the wall clock is kept deliberately loose because of
  it: `TIMED_OUT` scores 0, so a tight clock would book our own slowness as agent failure. The
  budget actually being scored is `max_steps`. Residual risk: the deadline is checked only
  *between* steps and the vendored model call retries with backoff, so a stalled call near the
  deadline could still be recorded as `TIMED_OUT` rather than `NOT_OBSERVED`.
- **Rank order is not graded.** A "top 3" task passes on any permutation of the correct pairs.
  Pairing is enforced; ordering is not.
- **`temperature = 1.0`**, because pass@k wants sampling diversity — so a run is not reproducible.
  The trajectory is the record, which is why `evaluator_version` and stored artifacts matter more
  here than reproducibility would.
