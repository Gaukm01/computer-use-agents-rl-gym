# Design — What We Built

A computer-use RL environment for Metabase: the gym, the agent, and the infrastructure that
runs rollouts against it and turns them into reward.

This document covers **what exists and why it is built that way**.
The productionisation plan is separate: [`PRODUCTION.md`](./PRODUCTION.md).

---

## 1. The shape of the problem

An RL environment needs three things, and each one has a failure mode that quietly invalidates
the numbers if you get it wrong:

| Part | The real requirement | The failure mode if you get it wrong |
|---|---|---|
| **Gym** | Every rollout starts from *identical* state | Rollout 5 sees rollout 1's leftovers; results drift and nobody notices |
| **Agent** | Acts through the same channel a human would | An API shortcut measures the wrong capability entirely |
| **Infra** | Reward reflects the agent, not the plumbing | Infra flakiness gets scored as incapability and poisons the dataset |

Every significant decision below traces back to one of those three columns.

**The one hard constraint: the agent is visual-only.** Screenshots in, mouse and keyboard out.
No REST API, no SQL connection, no DOM access. The *harness* does call Metabase's REST API — to
mint a session, and to check the browser is not sitting on a login page — because the harness
is infrastructure checking itself, never the policy perceiving its environment.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  HOST                                                                       │
│                                                                             │
│   React console  ──►  FastAPI  ──►  service.py  ──►  orchestrator           │
│   (web/dist)          :8000         (no web framework in sight)             │
│                          ▲                              │                   │
│                          │ polls runs/<job>/job.json    │ thread pool       │
│                          │                              ▼  (max 3)          │
│                          │                    ┌──────────────────────┐      │
│                          └────────────────────┤  ONE ROLLOUT         │      │
│                                               │  = one subprocess    │      │
│                                               │  + one private gym   │      │
│                                               └───────┬──────────────┘      │
│                                                       │                     │
│   ┌───────────────────────────┐         ┌─────────────▼─────────────────┐   │
│   │ Chromium (Playwright)     │  HTTP   │  DOCKER: one stack per rollout│   │
│   │ 1440x900, pre-authed,     │◄───────►│  :3001 / :3002 / :3003        │   │
│   │ domain-locked, recorded   │         │  ┌─────────────────────────┐  │   │
│   └───────────▲───────────────┘         │  │ metabase v0.54.15       │  │   │
│               │ PNG + url               │  │   └ H2 sample database  │  │   │
│               │                         │  └───────────┬─────────────┘  │   │
│   ┌───────────┴───────────────┐         │              │ JDBC           │   │
│   │ Gemini computer-use loop  │         │  ┌───────────▼─────────────┐  │   │
│   │ screenshot → model → act  │         │  │ postgres 16.9           │  │   │
│   └───────────────────────────┘         │  │   └ metabase_app        │  │   │
│                                         │  │     (restored from dump)│  │   │
│                                         │  └─────────────────────────┘  │   │
│                                         └───────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

> *Excalidraw to attach: `diagrams/architecture.png` — the same picture, with the "one stack
> per rollout" box multiplied x3 to make the concurrency visible at a glance.*

---

## 3. The gym

### Environment data is restored at runtime, per rollout

`resources/metabase_envdata.sql` is a `pg_dump` custom-format archive of a Metabase **app**
database — collections, dashboards, saved questions, users. Provisioning is four ordered steps:

1. `docker compose up -d postgres` — **Postgres only**
2. wait for `pg_isready`
3. `pg_restore` the dump into `metabase_app`
4. `docker compose up -d metabase`, then poll `/api/health`

**Step 1 is only Postgres for a specific reason.** A plain `compose up -d` starts Metabase the
instant Postgres reports healthy — that is what `depends_on: service_healthy` means — and
Liquibase would then build a fresh schema into the empty database that collides with the
restore. Starting Metabase *after* the restore removes the race structurally rather than
relying on timing. The manual workflow only ever got away with it because a human is slower
than Compose.

### One private stack per rollout, not one shared gym

Every rollout must start from identical state, and a shared gym cannot give that. We diffed a
live app DB against the dump after ~15 rollouts: Metabase writes back on its own as the agent
navigates, and some of what it writes is **visible to the next agent** — the "Pick up where you
left off" list on the home page had already lost a dozen seeded entries. That is progressive,
irreversible drift, not a shuffle that stabilises, and no targeted cleanup restores it.

Rather than build a reset primitive and then have to trust it, **each rollout boots its own
Postgres and its own Metabase, restores the dump, and destroys the stack**. Nothing is reused,
so nothing has to be cleaned. Contamination is impossible by construction rather than by
correctness of a reset path.

Two properties fall out for free:

- **The analytics data is isolated too.** `/plugins` is deliberately *not* volume-mounted, so
  Metabase's bundled H2 sample database lives in the container's writable layer and every
  fresh container extracts a pristine copy from the image.
- **Crash isolation.** A wedged JVM or a corrupted volume costs one rollout.

The cost is honest and measured: **33.6 s of provisioning per rollout**, against a ~285 s
rollout — roughly 12% overhead to eliminate an entire class of silent-corruption bug.

### Verifying the restore

Provisioning checks the seeded row counts before the agent starts, and records them on the
rollout's artifact. A partially restored database is wrong ground truth presented as a valid
run, so this fails the rollout rather than grading against incomplete data. Only tables
Metabase does not touch during its own boot can be checked this way.

### Teardown escalates, then recounts

The first version fired `compose down -v` and ignored the exit code. When the host disk filled,
`down` failed, six containers survived, and nothing noticed — and because the port returns to
the pool regardless, the *next* rollout on that port failed with "port already allocated". One
bad rollout poisoned every later one.

Teardown now escalates — `down` → `down` again → `docker rm -f` by project label → remove
named volumes — and then **recounts**, because the failure mode was precisely *a command
reporting success while doing nothing*. Failure is recorded on the artifact
(`teardown_error`) and never raised: a leaked container is an operational problem, not the
agent's fault, and the rollout's real result must survive.

---

## 4. The agent

We **subclass** `google-gemini/computer-use-preview` rather than fork it. Upstream's action
mapping (clicks, typing, scrolling, key handling, drag) is long, correct, and version-aware
across the modern and legacy tool sets, so it is reused verbatim. The loop around it is
replaced, for four reasons:

| Upstream behaviour | Why it cannot be used for rollouts |
|---|---|
| `except Exception: return "COMPLETE"` | A Gemini 5xx becomes indistinguishable from finishing the task — reward would correlate with API flakiness |
| `while status == "CONTINUE"` | No step cap; an unattended rollout could run forever |
| Safety confirmations call `input()` | A batch job would hang on a human prompt |
| No instrumentation | We need per-step screenshot, reasoning, latency and tokens |

### The answer channel

A `submit_answer` function tool is added alongside the computer-use tool. Calling it ends the
episode. Its description says only which function, which argument, and that it ends the task —
deliberately **no example and no format hint**, because working out what shape the answer
should take is part of what is being measured. Supplying it would be scoring our own
scaffolding.

### The environment boundary is an observation, not a missing tool

`excluded_predefined_functions=[]` — the agent may attempt any action. A domain lock refuses
off-origin *top-level* navigation and hands the refusal **back to the model as an
observation**, so it can adapt instead of silently repeating a no-op. Sub-resources (fonts,
CDN scripts Metabase itself pulls) are allowed; only main-frame navigation is blocked. The
environment has a boundary; we do not hand-tune what the agent is allowed to try.

### A malformed call is the agent's mistake, not ours

The model sometimes emits a call the tool schema cannot satisfy — `drag_and_drop` without an
`x`, for instance. An earlier version treated that as a harness failure, which ended the
rollout and **deleted it from the pass@k denominator**. Three rollouts in one job died that
way, one of them 18 steps into otherwise sensible work.

Now the raw exception is handed back as an observation and the episode continues. It costs a
step, so a model that keeps emitting bad calls still terminates — as `MAX_STEPS_EXCEEDED` with
reward 0, which is an honest verdict about the agent rather than about us.

### Step-budget awareness (a named ablation, on by default)

Each observation carries `steps_remaining`, and within 8 steps of the cap an explicit
instruction to submit. This exists because of an observed failure: an agent had the correct
value on screen at step 12, then spent 28 steps re-verifying whether a displayed `3.15` was
rounded, and hit the cap without submitting.

It is a recorded config field rather than a constant, because it materially changes what a run
measures — runs with it on and off are not comparable. **Measured honestly: it did not rescue
the case it was built for.** In the 50-rollout job, two rollouts ran to the 50-step cap and
submitted nothing while being warned from step 42 onward.

---

## 5. Reward

Reward is not decided by the agent or the executor. It falls out of a terminal **status**,
with one deliberate asymmetry.

```
      stop reason                  status                    reward
   ─────────────────────────────────────────────────────────────────
      SUBMITTED          ──►  ANSWERED ──grader──►  COMPLETED_CORRECT   1
                                   └──────────────► ANSWERED            0
      NO_FUNCTION_CALL   ──►  ANSWERED                                  0
      MAX_STEPS          ──►  MAX_STEPS_EXCEEDED                        0
      TIMEOUT            ──►  TIMED_OUT                                 0
   ─────────────────────────────────────────────────────────────────
      API_ERROR          ──┐
      HARNESS_ERROR      ──┼─►  NOT_OBSERVED                         null
      SAFETY_BLOCKED     ──┤         (excluded from pass@k entirely)
      GYM_PROVISION_FAIL ──┤
      GYM_AUTH_FAILED    ──┘
```

**`NOT_OBSERVED` is `null`, not `0`.** It means *we failed to observe an outcome* — the API was
exhausted, the browser died, the gym would not provision. Scoring that as 0 would make reward
correlate with infrastructure flakiness and quietly bias any dataset built from these
artifacts. It is the single most important decision in the project.

There is exactly one hand-off: the executor writes `ANSWERED` before grading, and only the
grader can promote it to `COMPLETED_CORRECT`. The reward function lives in one place.

> *Excalidraw to attach: `diagrams/reward-state-machine.png` — the diagram above, with the
> `null` branch visually separated from the `0` branch.*

### The grader

A pure function of `(artifact, task)`, with an `EVALUATOR_VERSION`, so stored trajectories can
be re-scored after a rule change without re-running a single rollout. It solves three real
problems in this task set:

1. **Formatting.** Metabase renders `101203.3` as `101,203.3`, and a model may echo `5` as
   `5.0`. Numbers are compared numerically with tolerance; only a string that is *entirely* a
   number is treated as one, so `"Q4"` and product titles stay text.
2. **Order.** `problem9` asks for a set of names where order carries no meaning, so lists
   compare as multisets.
3. **Pairing — the subtle one.** `problem1` returns `product_titles` and `ratings` as parallel
   arrays where element *i* of each corresponds. Comparing them independently as multisets
   would accept an answer with all the right titles and all the right ratings *paired wrongly*.
   Equal-length list values under a shared object are therefore zipped into tuples before
   comparison, so every value stays bound to its label.

Tolerance is confined to how the answer *arrives*; whether it is *right* stays strict. A
near-miss flag marks answers that are structurally correct but just outside tolerance, so a
human can look rather than trusting them as clean agent failures.

---

## 6. The infrastructure

### Three levels of parallelism, each forced by a different constraint

| Level | Unit | Mechanism | Why not something else |
|---|---|---|---|
| Job | T x K rollouts | `ThreadPoolExecutor` | Threads are correct *because* they only supervise — every worker parks in `Popen.wait()`, and Playwright never runs in this process |
| Rollout | one attempt | `subprocess.Popen` | Playwright's sync API is not thread-safe; a wall clock must be enforceable by SIGKILL; a crashed browser should cost one rollout |
| Gym | Postgres + Metabase | one compose project | Isolation must be per rollout, not per job |

**`ports.py` is the entire allocator, and the queue *is* the semaphore.** `claim()` blocks on
`get()` when all three ports are taken, so nothing anywhere needs a pool manager, slot table,
or lease bookkeeping. `MAX_GYMS = 3` is measured, not guessed: 4 vCPU / 7.74 GiB VM, ~1.08 GiB
Metabase RSS + ~54 MiB Postgres, so three stacks sit near 3.4 GiB with headroom for JVM boot
spikes. **Memory is the ceiling today, not API quota.**

**Teardown lives in the parent process, wrapping the subprocess — never inside the worker.** A
timeout kills the worker with SIGKILL, which runs no `finally`, so teardown placed there would
leak a container and a port on exactly the path that most needs cleaning up. It also keeps the
rollout's timeout a pure *agent* budget: provisioning is not charged against it.

### Progress needed no new plumbing

Both the parent (phases) and the worker (every step) already rewrite `artifact.json`. A
watcher thread reading those files is therefore the whole progress mechanism — no callback has
to cross the subprocess boundary. Two paths write job rows, and the split matters:

| Path | Source | When |
|---|---|---|
| `apply()` | the artifact the executor **returned** | rollout finished |
| `refresh()` | `artifact.json` **on disk**, every 2 s | rollout in flight |

`refresh()` skips slots already settled by `apply()`, so a live poll can never overwrite a
finished result — and `apply()` uses the value in hand, so a successful rollout cannot vanish
from the tally over an I/O error.

### Scheduling is task-major, and jobs are resumable

A task's K rollouts are all *submitted* before the next task's, so a half-finished job has
**complete, usable** results for some tasks rather than one sample of everything with nothing
settled. There is no barrier between tasks, so a task's last rollout overlaps the next task's
first. `job.json` carries a row for every one of the T x K rollouts from second one, which is
what lets the console render the full grid with queued slots greyed out.

Resume skips any rollout already in a terminal state, so an interrupted 30-rollout job costs
only the work that was actually lost.

### The failure that shaped the infrastructure

Two 30-rollout jobs died of host disk exhaustion. The first diagnosis — "the host is simply
full, `colima restart` fixes it" — was true and **not enough**, so it recurred at almost the
same point. The real cause:

```
~/.colima/.../datadisk   allocated: 23 GiB
inside the VM: /var/lib/docker   2.1 GiB used of 59 GiB  (4%)
```

23 GiB of host blocks holding 2.1 GiB of live data. The VM's data disk is ext4 mounted
**without `discard`**, so when Docker removes a container layer the blocks are freed *inside*
the guest filesystem and nothing ever issues TRIM to the virtual device. The host's sparse
image never gives them back, and ext4 scatters allocations, so each create/destroy cycle
touches ~1 GiB of *fresh offsets*, permanently.

`rm` inside a guest cannot shrink a host disk image. Our cleanup was at the Docker layer and
the growth was one layer below it. Three fixes shipped:

- `reclaim_host_disk()` runs `fstrim` after every teardown → host growth **1024 MB → 0.8 MB
  per rollout**, and each artifact records `host_image_gib` / `guest_used_gib` as a pair,
  because a single number is what made this invisible for a full job.
- A free-space floor stops a job **cleanly** with a stated reason instead of crashing at
  rollout 25.
- State writes now swallow `OSError`. This was the sharpest lesson: **the error path depended
  on the resource that failed** — the handler that recorded the job as FAILED wrote to disk,
  that write also failed, and the exception killed the supervising thread. The job then sat at
  `RUNNING` with nothing running.

What the incident proved is the point worth making: **the reward contract held under genuine
infrastructure failure.** 25 rollouts were graded, durable, and valid; 5 were `NOT_OBSERVED`
with reward `null`, excluded rather than booked as agent failures.

---

## 7. The console

Not pretty, and deliberately so — it exists to make rollouts legible.

- **New run** — upload a `tasks.json` (validated *before* it is stored, and the stored path is
  what the job actually runs), pick tasks, set attempts per task and concurrency, choose the
  model and sampling parameters, and see the worst-case wall clock before committing.
- **Job page** — polls every 2 s. A progress bar over passed / failed / not-observed / running
  / queued, and a per-rollout chip grid. Because provisioning takes ~35 s, an in-flight rollout
  shows a phase checklist (*booting gym → minting a session → attaching the browser → agent
  working, step N*) rather than looking hung. The agent config is read from the **job's own
  spec**, so the page reports the policy that produced the numbers, not the form's current
  state. The headline pass rate is withheld until the job ends — a rate over three of thirty
  rollouts is not the run's result.
- **Transcript** — one step per slide: the screenshot the agent saw, its reasoning, the action
  and arguments, refusals and malformed calls flagged distinctly. Plus a **Context** tab
  showing the exact first turn the agent received, a **Video** tab, and a grade panel with
  expected vs actual, the diff, and the near-miss flag.

> *Excalidraw to attach: `diagrams/rollout-lifecycle.png` — a swimlane across
> scheduler / gym / agent / grader / artifacts, with the phases the console displays marked on
> the gym and agent lanes.*

---

## 8. Results

Real numbers from the two most recent jobs, both on the current code.

**5 tasks x 10 attempts, concurrency 3 — 50 rollouts, 91 min, 21.8 M tokens**

| Task | pass@10 | Passed | Avg time | Avg steps |
|---|---|---|---|---|
| problem1 | yes | 10/10 | 241 s | 28 |
| problem3 | yes | 10/10 | 241 s | 31 |
| problem4 | yes | 10/10 | 215 s | 25 |
| problem5 | yes | 8/10 | 295 s | 34 |
| problem2 | **no** | **0/10** | 436 s | 49 |

pass@10 across graded tasks: **80%** · mean reward **0.76** · **0 not-observed**

**5 tasks x 3 attempts, concurrency 3 — 15 rollouts, 27 min, 7.1 M tokens**
pass@3: **100%** of tasks · mean reward **0.93** · **0 not-observed**

Cost anatomy, because it drives everything in `PRODUCTION.md`: **~500k tokens per rollout at a
prompt:output ratio of ~500:1.** This is entirely a screenshot problem — three 1440x900 PNGs
retained per turn, ~13k prompt tokens per step.

### Why `problem2` can never pass

Its expected counts (996 / 700 / 664) are **larger than any product's order count in this
database** — the busiest product has 174 orders. We checked three readings of the prompt with
SQL against the gym's own sample data; none reproduces the answer, and none of the three named
products even carries a rating of 5. Independently, **9 of the 10 provided answers do reproduce
exactly**, which is what makes this one identifiable as a task-level discrepancy rather than a
grader bug.

**Action taken in the platform: none.** The answer stays exactly as provided and `problem2`
keeps being graded against it. This note exists so a 0% pass rate is attributed to the task
rather than to the agent. Two other tasks (`problem4`, `problem10`) pass *only* because the
grader has numeric tolerance — the provided answers are the rounded, displayed values while
the data is full precision.

---

## 9. What we would add next

Deliberately not built, and each one is a small, well-understood piece of work rather than a
rethink.

**1. Seed validation as an advisory signal, not a hard gate.**
Today a row-count mismatch raises during provisioning and the rollout becomes `NOT_OBSERVED`.
The better version records the counts and a boolean on the artifact and lets the rollout
proceed, so drift becomes a *trust score* on the result rather than a failure of it — and a
systematic drift shows up as a trend instead of an outage.

**2. A ground-truth correctness checker.**
When a task fails on every attempt, the interesting question is whether the *agent* or the
*task* is wrong. We answered that for `problem2` by hand, with SQL, outside the platform. The
product version is a checker that recomputes each task's expected answer from the live
environment and diffs it against the task file, so environment drift or a bad answer surfaces
as a failing check rather than as a mysterious pass-rate drop. Not built because it needs tasks
to carry the *query* that defines their answer, which is a bigger change to the task format
than it first appears.

**3. Reuse a pre-warmed Metabase stack instead of booting one per rollout.**
Provisioning is 33.6 s of every ~285 s rollout. A small pool of pre-started stacks, leased and
returned, would hide almost all of it — the boot happens before the rollout is dequeued rather
than inside it. Not built because reuse reintroduces exactly the contamination question that
per-rollout stacks removed, and correctness came first at this scale. It is the natural first
optimisation once writes and reset are designed properly; covered in
[`PRODUCTION.md`](./PRODUCTION.md).

**4. Retriggering failed rollouts.**
Job-level resume exists and works, but only from the CLI, and it treats `NOT_OBSERVED` as done
rather than retrying it. What is missing is the obvious operator action: *re-run just the
rollouts whose outcome was never observed*, from the console, into the same job. Not built
because it needs a decision about identity — a retry is a new rollout with a new trajectory,
so either the slot is overwritten (losing the failed attempt's record) or the job grows a
second row for the same K index.

**5. Tool clock vs wall clock.**
`duration_s` today is wall clock: model latency, network, browser actions, and page loads all
in one number. That makes it useful for capacity planning and useless for reasoning about the
agent. A tool clock would separate per-turn *agent* time (model call) from *environment* time
(action execution and page settle) — both are already measured per step as `latency_ms`, they
are simply summed. Splitting them would let us answer "is this task slow because the model
thinks hard or because Metabase is slow?", and would make the timeout budget enforceable
against agent time rather than against infrastructure.

---

## 10. Known limitations

Honest list, because these are the things a reviewer will find.

- **Jobs are designed to run one at a time.** Starting a job sweeps orphaned rollout
  containers, which would also remove a concurrently running job's gyms; the console confirms
  before starting a new job while one is running. Scoping the sweep per job is the real fix.
- **The running-job registry is in memory.** Restarting the API abandons in-flight jobs.
  Artifacts and `job.json` survive and `--resume` recovers them, but the abandoned job keeps
  reporting `RUNNING` until it is resumed — there is no read-side reconciliation, and no
  Resume button in the console. This is deliberately left to the production design rather than
  faked locally.
- **The timeout path has never fired.** Zero `TIMED_OUT` across 121 recorded rollouts, and no
  test drives the SIGKILL path. There is also a known latent mislabel: the agent checks its
  deadline only *between* steps, and the vendored model call retries 5x with exponential
  backoff, which can exceed the parent's 45 s shutdown grace — so a stalled API call near the
  deadline would be recorded as `TIMED_OUT` (reward 0) when the honest classification is
  `NOT_OBSERVED` (reward null).
- **Concurrency is capped at 3** by VM memory. Correct for this machine; not a general limit.
- **Rank order is not graded.** List comparison is order-insensitive, so a "top 3" task passes
  on any permutation of the correct three pairs. Pairing *is* enforced; ordering is not.
- **`temperature = 1.0`** by default, because pass@k wants sampling diversity — which means a
  run is not reproducible. The trajectory is the record, which is why `evaluator_version` and
  stored artifacts matter more here than reproducibility would.
- **Safety confirmations are auto-acknowledged.** Unattended rollouts cannot block on a human.
  Recorded per step and counted per rollout, because auto-approving a safety check is a policy
  choice and not an implementation detail to bury.
