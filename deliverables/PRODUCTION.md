# Productionisation Plan

Two questions to answer:

1. **What if the agent writes in addition to reads?**
2. **What if we want thousands of rollouts?**

They look independent. They are not — they meet at one place: **the unit of isolation and who
owns its lifecycle.** Writes make environment state part of the reward signal, so the
environment has to outlive the agent and be provably unshared. Scale changes who coordinates
that, because one process holding a thread pool over a port queue cannot run thousands of
rollouts.

Everything below is anchored to numbers we measured on the local platform, because reasoning
about scale without them produces confident nonsense.

| Measured | Value |
|---|---|
| Tokens per rollout | **~500 k** (413 k–558 k observed) |
| Prompt : output ratio | **~500 : 1** — screenshots dominate completely |
| Wall clock per rollout | **~285 s** (215 s–436 s by task) |
| Gym provisioning per rollout | **33.6 s** (~12% of a rollout) |
| Metabase RSS / Postgres RSS | 1.08 GiB / 54 MiB |
| Local concurrency ceiling | **3**, bounded by VM memory |

**The consequence that frames the whole plan:** 1000 rollouts ≈ **500 M tokens** and **~83
hours** of single-stream wall clock. Parallelism is not an optimisation at that point; it is
the only thing that makes the number finite. And the binding constraint is **model quota, not
machines.**

---

## Problem 1 — The agent starts writing

### What breaks

Today every task is read-only and the reward is a comparison of two JSON values. The moment
tasks are *"create a question named X in collection Y"*, three things break at once:

1. **Reward moves out of the transcript and into the database.** "Submitted the right answer"
   stops being a proxy for "did the work". A rollout can answer correctly and have created
   nothing.
2. **Contamination becomes a correctness bug.** Two rollouts sharing a gym now see each other's
   dashboards. The results are still *produced*, just wrong — the worst failure mode there is.
3. **The environment must survive until after grading.** If teardown is eager, the evidence is
   destroyed and results come back mysteriously empty.

### The isolation decision

| Option | Pro | Con |
|---|---|---|
| **A. Warm pool + reset between rollouts** | Amortises the boot; pool ≈ concurrency x 1.2 | Reuse means contamination is *possible* — you are trusting reset to be total. Adds leases, timeouts, reapers, poisoned-instance detection |
| **B. Immutable environment per rollout** | Contamination impossible **by construction**; zero reset code; crash isolation free | Pays the boot on every rollout (~12%) |
| **C. B, plus a warm buffer of pre-started environments** | Both properties | Idle capacity; a little scheduler complexity |

**Recommendation: B, moving to C once measured.** The argument to make out loud is about
falsifiability: at thousands of rollouts *"the reset was total"* is unfalsifiable, while
*"nothing was reused"* is verifiable. 12% overhead to delete an entire class of silent
corruption is cheap, and "no reset code" is worth more than it sounds — reset bugs do not
crash, they corrupt results.

C is the honest production answer: keep environments immutable, but pre-start a small buffer so
the boot happens *before* a rollout is dequeued rather than inside it. That recovers most of the
33.6 s without reintroducing reuse.

### How the pristine-copy setup changes

Locally, "pristine" means `pg_restore` into a fresh Postgres, ~2 s, once per rollout. Three
changes for production:

**1. Bake the restored state into images instead of restoring at boot.** Build a
`metabase-app-db:seed-<hash>` image whose data directory is *already* the restored dump, and a
matching Metabase image. The rollout then starts two containers and waits, with no restore step
at all. The dump hash goes in the tag, so which seed a rollout ran against is recorded rather
than assumed.

**2. Make the analytics database per-rollout too.** Today the analytics data is Metabase's
bundled **H2** sample database, isolated only incidentally — `/plugins` is not volume-mounted, so
each container extracts a fresh copy from the image. That is fine for reads and unclear for
writes: we never verified whether a Metabase native query can `INSERT`/`UPDATE`/DDL against it.
Under writes that ambiguity is unacceptable, so replace it with a **per-rollout Postgres
restored from a template**, and re-point the seeded database-connection row at it. Two benefits:
the reset story becomes uniform across both databases, and the grader can assert against the
analytics data with plain SQL. Cost: the connection row in the dump has to be rewritten at
image-build time.

**3. Both databases die with the rollout.** No `TEMPLATE` clone, no `DROP`/`CREATE`, no reset
path to trust. Worth stating why the tempting optimisation is skipped: **Metabase caches app
state in memory, so you cannot swap the app DB under a live JVM.** A `TEMPLATE` clone would
remove a ~2 s restore from a reset dominated by a ~40 s JVM restart. **The restart is the
ceiling, so optimising the restore is optimising the wrong thing.**

### The lifecycle, and why the order is load-bearing

```
   acquire environment  ──►  execute rollout  ──►  GRADE  ──►  release environment
                                                    ▲
                                     the evidence lives HERE, in the dirty environment.
                                     Release before grading and results come back empty.
```

Locally the ordering is free because nothing resets. With writes it is a hard constraint, and
it is easy to get wrong because the failure is silent — an eager teardown produces *zero
findings*, not an error.

Two follow-on choices:

- **The grader runs colocated with the environment**, as a final step in the same pod. It can
  then reach both databases directly without exposing them, and it cannot outlive the evidence.
- **Snapshot the state evidence per rollout.** `evaluator_version` exists so stored
  trajectories can be re-scored after a rule change. For write tasks that only works if the
  *evidence* was stored, not just the answer — so capture the targeted query result sets (not a
  full dump) alongside the trajectory. **Without this, re-grading silently stops applying to
  exactly the tasks that need it most.**

### How grading changes

The current grader compares a submitted dict against an expected dict. Right for reads, useless
for "create a question named X".

| Option | Pro | Con |
|---|---|---|
| **A. SQL assertion per task** — the task file carries a query + expected rows | Maximally flexible; an escape hatch for anything | Task authors now write SQL; the task file stops being portable |
| **B. Declarative state spec** — `{"report_card":[{"name":"X","collection":"Y"}]}`, interpreted by the grader | Portable; the task file stays data | Needs a mini query language; covers only what we anticipated |
| **C. Before/after diff with allow-listing** | Catches *unintended* damage — did it delete something? | Very noisy: Metabase writes `query_execution`, `task_history`, `recent_views` constantly |

**Recommendation: B for the common cases, A as the escape hatch, and C narrowly — as a
side-effect *detector* over an allow-list of content tables only (`report_card`,
`report_dashboard`, `collection`, permissions), reported as a separate signal that never enters
reward.** That last split is the design point: folding noisy diff output into pass/fail is how
you end up with a reward function nobody trusts.

Two schema consequences:

- **Reward becomes a conjunction:** answer-match **AND** state-assertions. Both are recorded
  separately, because *"right answer, did not do the work"* is a distinct and interesting
  failure that a single boolean would hide.
- **A new terminal status is needed** for it, alongside the existing set.

### Blast radius

All 12 seeded users are admins. Bounded today only because tasks are read-only. A writing
admin agent can delete dashboards, alter permissions, or edit the database connection — which
in an immutable-pod world is harmless, because the pod dies anyway. **That is another argument
for option B.**

It does interact with grading, though: if a rollout trashes the environment, a grader query
that assumes seeded rows exist will *throw* rather than fail cleanly. Grader assertions must
tolerate a destroyed environment and report it as a failed rollout, not a crashed grader.

> *Excalidraw to attach: `diagrams/write-lifecycle.png` — the swimlane above, with the grader
> inside the environment boundary and the "release" arrow after it.*

---

## Problem 2 — Thousands of rollouts

### What breaks

- **One process coordinates everything.** The thread pool, the port queue, and the job registry
  all live in one Python process. Restarting it abandons in-flight jobs.
- **`job.json` is the source of truth and has exactly one writer.** That is the only reason it
  works. The moment workers are distributed it must become **derived state**.
- **The retry unit is wrong.** Retry, distribution, and leasing all want to happen at the
  *rollout*, not at the job.

### The scheduling and queueing service

| Option | Pro | Con |
|---|---|---|
| **A. Scale the current thread pool vertically** | No new infrastructure | Does not reach thousands; a single failure domain |
| **B. Rollout queue + stateless workers** (SQS or Redis + Postgres) | Right granularity; well understood; workers idempotent on `attempt_id` | We own retry and visibility-timeout semantics |
| **C. Workflow engine (Temporal)** | The rollout lifecycle is a saga; "release the environment even if grading crashed" is exactly what it is for | A whole operational surface for one guarantee |

**Recommendation: B.** The shape:

```
   POST /jobs
      │  explode into T x K rollout rows  (status=QUEUED)
      ▼
   ┌──────────┐    lease (visibility timeout)   ┌────────────────────────────┐
   │ rollout  │◄────────────────────────────────┤ worker (stateless, N of M) │
   │  queue   │                                 │  1. admission gate (quota) │
   └──────────┘                                 │  2. acquire environment    │
        ▲                                       │  3. run agent loop         │
        │ nack / redrive                        │  4. grade in-place         │
        │                                       │  5. push artifacts to S3   │
   ┌────┴─────────────┐                         │  6. write rollout row, ack │
   │ results DB       │◄────────────────────────┤  7. release environment    │
   │ job = projection │                         └────────────────────────────┘
   └──────────────────┘
```

- **The rollout is the unit** of queueing, leasing, retry and distribution. A job is a row that
  fans out and a view that fans back in.
- **Workers are stateless and idempotent on `attempt_id`.** A redelivered message re-runs a
  rollout; it never corrupts one.
- **Job state is a projection** over rollout rows — `pass@k`, mean reward and the T x K grid are
  all computed on read. The local platform already recomputes derived numbers on read, so this
  is a change of storage, not of semantics.
- **Explicitly not Temporal**, and worth saying why in the demo: the only compensation this
  saga needs is *destroy the environment*. If the environment is a pod with an owner reference
  and a TTL, **the cluster's garbage collector is the compensation.** Choosing the isolation
  unit well removes the need for the saga engine.

### The pool of machines

Sizing is arithmetic, not guesswork. A rollout is ~285 s of wall clock plus ~34 s of
provisioning, and needs ~1.2 GiB for its Metabase + Postgres plus a browser (~0.5 GiB):

```
   rollouts/hour per lane        = 3600 / 320s          ≈ 11
   lanes needed for 1000 in 4h   = 1000 / (4 x 11)      ≈ 23 concurrent rollouts
   memory needed                 = 23 x ~1.8 GiB        ≈ 42 GiB
   ⇒ ~6 x 8-vCPU / 32 GiB nodes, or ~3 larger ones
```

Two layers, and they should not be confused:

- **A node pool** (VMs) sized for aggregate memory and autoscaled on queue depth. Nodes are
  cattle; nothing rollout-specific lives on them.
- **An environment buffer** — K pre-started rollout environments kept warm ahead of the queue,
  so a dequeued rollout does not wait for a JVM. Size it at roughly one boot-time's worth of
  arrivals (~35 s x arrival rate). This is option C from Problem 1, and it is where the 12%
  provisioning overhead goes to die.

**One node per rollout at the extreme is worth mentioning as the clean answer to the disk
problem we hit locally.** Our host-disk exhaustion was caused by per-rollout container layers
growing a shared sparse disk image that `rm` inside the guest could never shrink. When the unit
of isolation is an ephemeral machine with its own disk, that entire failure class disappears —
the disk goes away with the rollout.

### What actually binds: quota, not machines

At 23 concurrent rollouts, each making a model call per step with ~34 steps per rollout, that
is a sustained call rate one API key almost certainly cannot serve. Defences, in order:

1. **SDK-level backoff** — already there, 5 retries with exponential delay, absorbing transient
   429s *below* the rollout boundary. This is why rollout-level retry is off.
2. **A global token bucket** in Redis, so concurrency is throttled by *observed* quota rather
   than a hard-coded number.
3. **Backpressure at the dequeue point** — if the 429 rate crosses a threshold, stop leasing
   rather than burning retries inside running rollouts.
4. **Multiple keys or projects with rotation.** Operationally annoying; often the only real
   answer.

**The subtlety worth saying out loud: rate-limit failures are correlated.** They hit a whole
job at once, not scattered randomly the way K independent attempts assume. So K attempts
provide *no* redundancy against quota exhaustion, which is precisely why backpressure belongs
at the queue rather than at the rollout.

### Cost, and the fact that images dominate

~500 k tokens per rollout at ~500:1 prompt:output is *entirely* a screenshot problem — three
1440x900 PNGs retained per turn, ~13 k prompt tokens per step. Levers, ranked by
payoff-to-risk:

1. **Retained screenshot count** (currently 3 turns) → 2 is ~33% off the prompt bill. Risk: the
   agent loses the context to notice it already tried something.
2. **Downscale screenshots before sending** while keeping the browser viewport large. Best
   ratio in principle; needs a legibility check first.
3. **Loop detection and early stop.** We observed a real 28-step verification spiral — ~350 k
   wasted tokens in one rollout. Interesting beyond cost: a loop detector is a *better failure
   signal* than "hit the step cap".
4. **Model tiering.** Five computer-use models are reachable and we have only ever run one.
   Could be a large cost win or a large accuracy loss — unmeasured, and it should be presented
   as unmeasured.
5. **Viewport reduction last**, because a cramped Metabase UI increases step count and can be
   net-negative.

> *Excalidraw to attach: `diagrams/scale-architecture.png` — control plane vs data plane, with
> the admission gate on the dequeue path and the measured numbers in a corner box.*

---

## Problem 3 — Storage and state

### Where we are

Everything is the filesystem. `runs/<job_id>/job.json` plus one directory per rollout holding
`artifact.json`, `evaluation.json`, `spec.json`, ~34 screenshots and a video. It works locally
for a good reason — the artifact is rewritten after every step, so a killed rollout keeps its
trajectory and a UI can follow along by polling a file, with no websockets and no database.

It stops working for two reasons, both structural rather than aesthetic:

- **`job.json` has one writer.** With distributed workers it cannot be authoritative.
- **Media does not belong on a worker's disk.** At 1000 rollouts that is ~34 k screenshots and
  1000 videos — hundreds of GB, on machines that are supposed to be disposable.

### Where it goes

**Postgres for state, object storage for bytes.**

| Today | Production |
|---|---|
| `runs/<job>/job.json` | `jobs` table + a **view** over `rollouts` — never a written aggregate |
| `<attempt>/artifact.json` | `rollouts` row (status, phase, reward, tokens, timings) + trajectory as `jsonb` or an S3 object |
| `<attempt>/evaluation.json` | `evaluations` row, keyed by `(attempt_id, evaluator_version)` so a re-grade **adds** a verdict instead of overwriting one |
| `steps/*.png`, `video/*.webm` | S3, with the row storing keys rather than paths |
| — | `state_snapshots` — the post-run evidence write tasks need for re-grading |

Three properties worth keeping deliberately:

- **The rollout row is the unit of truth; the job is a projection.** This is what the local
  platform already does on read; production just moves where the rows live.
- **Verdicts are append-only.** `evaluator_version` exists so stored trajectories can be
  re-scored after a rule change; that only has value if old verdicts stay attributable to the
  rules that produced them.
- **Retention is a policy, not an accident.** Keep failures indefinitely, sample successes,
  expire the rest. Question whether to keep video at all — it is the largest artifact and it
  largely duplicates the per-step screenshots, so *video on failure only* is likely the right
  default.

Deliberately not designing the schema further here. The point is the shape: **state in a
database, bytes in object storage, job numbers derived rather than written.**

---

## Problem 4 — Trusting a thousand numbers

Scale makes individual transcripts unreadable, so aggregates become the product. Four things
have to exist or the numbers quietly stop meaning anything.

- **`NOT_OBSERVED` rate as a first-class SLI, with its own alert.** It is excluded from pass@k
  by design, which is correct — and that means a rising infrastructure failure rate is
  *invisible* in the headline number. Our two disk exhaustions are the proof: 5 of 30 and 5 of
  30, correctly scored `null`, and nothing in a pass rate said so.
- **Ground-truth drift detection.** Expected answers were computed against some sample-data
  vintage; the data is time-relative, and any Metabase bump can silently change them. Tasks
  should carry the *query* that defines their answer, and a scheduled job should recompute and
  diff against the task file — so drift surfaces as a failing check, not as a mysterious
  pass-rate drop. `problem2` is the live example of why this matters.
- **Flake quarantine.** A task whose pass rate is neither ~0 nor ~1 across many attempts is
  either genuinely hard or badly specified. The judgement is human; surfacing the candidates is
  not.
- **Record the served model *version*, not the alias.** Aliases move underneath you. Cheap to
  capture now, impossible to recover later.

---

## Explicitly out of scope

Named so the omissions read as decisions:

- **Kubernetes manifests, Terraform, CI/CD, autoscaling policy maths.** Deployment mechanics,
  not design.
- **Platform multi-tenancy, RBAC, quotas, billing.** One team, one key, for now.
- **The RL training loop itself.** This is a rollout collector. The only seam that matters is
  that artifacts become a dataset and `reward = null` rows must be excluded from it.
- **Reverse-engineering which Metabase caches make a hot app-DB swap safe.** The payoff is real
  (reset would get ~20x cheaper) and so is the risk. Under immutable environments the question
  disappears, so we decline it rather than budget for it.
- **A general-purpose workflow engine.** See the Temporal argument above.

---

## Migration order

Smallest useful step first; each one is independently valuable.

1. **Baked seed images + a per-rollout analytics Postgres.** Unlocks writes and removes the
   restore from the critical path. Nothing else has to change yet.
2. **`grade → release` ordering, plus the state snapshot.** This is what makes write-task
   grading correct and re-gradable. Skip the snapshot and re-grading silently stops working for
   write tasks.
3. **Postgres for rollout state; job numbers become a view.** Required before workers can be
   distributed, and it fixes the orphaned-`RUNNING` job we have locally for free.
4. **Rollout queue + stateless workers + S3 artifacts.** Now horizontal scale is possible.
5. **Admission control on observed quota, and the warm environment buffer.** Now it is
   *efficient* — and this is the step where quota, not machines, becomes visibly the ceiling.

Steps 3 and 4 in the wrong order is the one real trap: distributing workers while `job.json` is
still authoritative gives you a multi-writer file and results that disagree with themselves.
