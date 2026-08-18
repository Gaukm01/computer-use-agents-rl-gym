# Productionisation Plan

## What production means here

The MVP runs on one machine: a thread pool, fixed ports, files as the source of truth, read-only
tasks. Production must answer two things it cannot — **the agent writes**, and **thousands of
rollouts** — and both change how an environment is created, graded, and destroyed.

| | Front | Question |
|---|---|---|
| **The agent writes** | 1 | Where do writes land, without touching the seed? |
| | 2 | How is a write task graded, and when? |
| **Thousands of rollouts** | 3 | How do rollouts get scheduled and executed? |
| **Foundations** | 4 | Where does state live once one machine is not enough? |
| | 5 | How do we know the numbers still mean anything? |

Storage (Front 4) is the precondition for the rest; it sits fourth because it is enabling work,
not the interesting part. Design level only — no figures, since none of this has been run.

---

## Front 1 — Write tasks: database isolation

**Problem.** The seed must never be mutated, and no rollout may see another's writes.

**Approach.**

- One read-only **template** database per machine, built from the seed dump, tagged by version.
- Rollout start: `CREATE DATABASE rollout_<id> TEMPLATE seed_<version>` — a file-level copy, so
  no dump or restore sits in the rollout's critical path.
- That rollout's Metabase points at the copy; every write lands there.
- Rollout end: environment torn down, database dropped. Nothing ever connects to the template.
- The **analytics** database follows the same rule — a write task can touch either.

**Consequences.**

- **Auth comes after the copy.** Metabase keeps sessions in its app database, so each copy
  invalidates them. A warm environment (§3.4) must be warmed *after* its copy exists.
- **Blast radius is contained.** Seeded users are admins and can wreck anything — harmless when
  the database dies with the rollout, but the grader must tolerate a wrecked environment rather
  than crash on it.

**Not chosen.** Reset-in-place between rollouts. Correctness would then depend on the reset being
total, and a missed table corrupts results silently instead of failing loudly.

---

## Front 2 — Write tasks: grading

**Problem.** The result lives in the database, and the environment dies with the rollout — so
grading cannot happen afterwards.

**Approach.** Lifecycle becomes **acquire → execute → grade → release**, with the grader running
inside the environment while it is still live.

- **Did the write happen?** The copy began as the template, so the template *is* the "before".
  Finding the agent's work is a set difference over content tables — no snapshot needed at start.
- **Is it correct?** Those rows are checked against the task's expectation: name, location, and
  the fields that must match.
- **Reward = answer AND state.** *"Right answer, nothing created"* is a failure, recorded as its
  own outcome rather than folded into one boolean.
- **Expectations are declarative** in the task file, with a raw SQL assertion as the escape
  hatch — so a task file stays data rather than becoming a program.
- **Evidence is stored** with the verdict: the rows the grader read, since the database they came
  from will not exist when someone wants to re-grade.

**Not chosen.** Diffing the whole database before and after. It would catch unintended damage
too, but Metabase writes to its own tables constantly, so the allow-list becomes the real spec.

---

## Front 3 — Scale: scheduling and disposable environments

**Problem.** Thousands of rollouts cannot run from a thread pool on one machine.

**3.1 Queue rollouts, not jobs.** A job explodes into rollout rows; the queue holds rollouts. A
worker leases one, claims a free slot, runs the agent, grades in place, writes the result,
releases the slot, acknowledges. A job becomes a view over its rows. Retry is queue redrive when
a worker dies — never a retry inside a rollout.

**3.2 Disposable VMs with a slot cap.** Each VM hosts a bounded number of environments; the cap
comes from measuring memory per environment on the chosen instance type. A rollout takes a free
slot, runs, and releases it to the next in queue. Slots are held on a **lease with a timeout**, so
a dead worker's environment is reclaimed rather than leaked. VMs hold nothing rollout-specific.

> Daytona, E2B and Firecracker-based runners look like a fit for this shape. Not evaluated — a
> starting point for comparison, not a recommendation.

**3.3 How concurrency scales.** Total concurrency is **VMs × slots per VM**. Scale by adding VMs,
not by raising the cap — the cap is a memory property of the environment and does not move. Queue
depth drives how many VMs are up, so a burst adds machines instead of slowing running rollouts.

**3.4 Optimisation: a pre-warmed pool.** Keep environments already copied, seeded, authenticated
and with the browser attached, idle and waiting. A rollout then starts at its first model call
instead of paying boot, copy and login. Pool size trades idle capacity against queue latency.

**3.5 What actually limits throughput.** The model API becomes the ceiling before machines do,
and its failures are **correlated** — they hit everything at once, so repeated attempts give no
protection. Throttle where work is leased: when the API starts refusing, workers stop taking new
rollouts rather than burning retries inside ones already running.

**Not chosen.** One fresh VM per rollout. Cleanest isolation, but every rollout pays a machine
boot and VM churn becomes the bottleneck first.

---

## Front 4 — Storage and state

**Problem.** State is files on one machine, and it works *because* exactly one process writes it
— which stops being true the moment work is distributed.

**Approach.**

- `jobs` and `rollouts` tables hold status and results. Job-level numbers are computed by
  aggregating rollout rows, never written, so there is nothing to keep in sync.
- Grades are separate rows — a re-grade adds a verdict instead of overwriting one.
- Screenshots, video and trajectories go to object storage; the row holds the key.
- The in-memory job registry becomes a durable queue with a visibility timeout (managed queue, or
  Postgres `FOR UPDATE SKIP LOCKED`). At-least-once delivery is safe because workers are
  idempotent on rollout id.

**Not chosen.** Files as the source of truth with a database as an index over them — two writers,
two truths, and permanent reconciliation.

---

## Front 5 — Validation, tracking and analytics

**Problem.** At scale nobody reads transcripts, so aggregates become the product — and a broken
environment looks exactly like a weak model.

**Approach.** Three signals tracked separately from the pass rate:

- **Infrastructure-failure rate**, alerted on its own — it is excluded from the score, so a
  degrading fleet is otherwise invisible.
- **Per-task pass rate over time**, so environment drift reads as a task that stopped passing
  rather than as a worse model.
- **A scheduled ground-truth check** recomputing each task's expected answer against a fresh
  environment, so a bad task fails a check instead of quietly lowering a score.

---

## Order of work

1. Storage and the rollout queue — everything else assumes them.
2. Template database plus per-rollout copies.
3. `grade → release` and the write-aware grader.
4. Disposable VMs with slots and leases; the warm pool once it is worth measuring.
5. Tracking and drift checks.

Out of scope: deployment mechanics, multi-tenancy and access control, and the training loop —
this platform collects rollouts, it does not consume them.
