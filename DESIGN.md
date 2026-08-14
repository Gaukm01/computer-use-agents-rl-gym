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
| **Agent** | Gemini computer-use driving a real browser against the gym | 🟡 pieces verified, loop not wired |
| **Rollout infra** | Task definitions, resets, grading, running many attempts | ⬜ not started |

**The one hard constraint: visual only.** The agent sees screenshots and emits clicks and
keystrokes. No REST API calls, no SQL, no DOM inspection. If something looks easier via API,
that's a signal to flag it, not to take the shortcut — an API path would measure the wrong
thing entirely.

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
| Is the gym up? | `docker compose ps` · `curl -s localhost:3000/api/health` |
| Is my API key good? | `cd agent && ./.venv/bin/python verify_gemini.py` |
| Can the browser see the gym? | `cd agent && PLAYWRIGHT_HEADLESS=1 ./.venv/bin/python verify_harness.py` |
| Inspect the seeded data | `psql -h localhost -p 5433 -U postgres -d metabase_app` |
| Run the agent (gated) | `cd agent/computer-use-preview && ../.venv/bin/python main.py --query "…" --env playwright --initial_url "http://localhost:3000" --highlight_mouse` |

`--highlight_mouse` draws a red circle at each click — worth it when watching a run.

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
🟡 Agent loop   upstream main.py runs; not wired into anything of ours
⬜ Tasks        tasks.json untouched and unscoped
⬜ Grading      no success criteria defined
⬜ Reset        TEMPLATE approach decided, not implemented
⬜ Runner       no orchestration, no parallelism, no logging
```
