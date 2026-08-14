# Setup Progress

> **Docs map** — [`DESIGN.md`](./DESIGN.md): architecture, decisions, open questions (start
> here) · [`INFRA_KNOWLEDGEBASE.md`](./INFRA_KNOWLEDGEBASE.md): exhaustive command reference ·
> **this file**: chronological log of what was done, verified, and corrected.

## Status: ✅ COMPLETE — gym foundation verified end to end

All three steps done and verified with real command output, and **you confirmed the UI check
manually: login works and the dashboards render.** Underneath that, I executed three seeded
dashboard cards through the API and they returned real data (see Step 3).

> Phase 2 (agent setup) is below. Jump to [Phase 2](#phase-2-agent-setup--visual-computer-use-only).

Full infra reference (every install, env var, and startup command) lives in
[`INFRA_KNOWLEDGEBASE.md`](./INFRA_KNOWLEDGEBASE.md).

## Completed

- [x] **Step 1 — Environment check**

      Nothing container-related existed on this machine. Installed with your approval:
      `colima 0.10.3`, `docker 29.7.2` (client), `docker-compose 5.4.0` (plugin).

      Verified output:
      ```
      Docker version 29.7.2, build a7dcaa6fdb
      Docker Compose version 5.4.0
      Server: 29.5.2 | OS: Ubuntu 24.04.4 LTS | Arch: aarch64 | CPUs: 4 | Mem: 8307101696
      pg_restore (PostgreSQL) 18.4
      ```

      Two wiring issues found and fixed:
      - Homebrew's compose plugin isn't on the docker CLI's search path → had to add
        `cliPluginsExtraDirs` to `~/.docker/config.json`, else `docker compose` doesn't exist.
      - `libpq` is **keg-only**, so `psql`/`pg_restore` look missing but live at
        `/opt/homebrew/opt/libpq/bin/`.

      Directory skeleton created: `metabase/`. (`agent/`, `infra/`, `tasks/` deliberately
      not created — out of scope.)

- [x] **Dump inspection** (read-only; neither .sql file modified)

      `resources/metabase_envdata.sql` confirmed a real PGDMP custom-format archive:
      Format CUSTOM, gzip, Dump Version 1.15-0, source db `root_db`, owner `postgres`,
      dumped from **PostgreSQL 16.9**, created 2025-07-09, 1098 TOC entries, requires the
      **`citext`** extension (bundled in the official `postgres` image).

      **Finding 1 — the dump is self-contained.** `metabase_database` has exactly one row:
      Metabase's own bundled H2 sample database
      (`file:/app/plugins/sample-database.db`, engine `h2`, `is_sample=t`). No external
      data warehouse needs provisioning; the seeded dashboards query the same sample
      dataset that ships inside the Metabase image.

      **Finding 2 — schema is Metabase v0.54-era.** Newest Liquibase changeset is
      `v54.2025-03-28T11:22:01`. A newer minor (v0.55+) would auto-migrate the schema and
      permanently alter the seeded env, so the image is pinned to **`v0.54.15`** — the patch
      current on the dump's creation date. Reasoning and tag timeline in the knowledgebase.

      Expected content, to check against after restore:

      | rows | table | | rows | table |
      |------|-------|-|------|-------|
      | 2997 | query_execution | | 14 | collection |
      | 475 | databasechangelog | | 12 | core_user |
      | 86 | report_card | | 8 | metabase_table |
      | 81 | report_dashboardcard | | 6 | report_dashboard |

      6 dashboards: E-commerce Insights, Revenue Dashboard, Product Dashboard,
      Customer Funnel Dashboard, Discount Information Dahboard [sic], Personal Analytics.
      12 users, all `@deeptune.com`, all superusers, all sharing one bcrypt hash.

- [x] **Step 2 — Metabase running standalone on Postgres, vanilla sample data**

      Compose file: `metabase/docker-compose.yml` (project `metabase-gym`).
      **Port: http://localhost:3000.** Postgres exposed on host **5433**.

      ```bash
      cd /Users/gkumar/assessments/hackathon/metabase
      docker compose up -d
      ```

      Application DB is Postgres, not H2 — confirmed from Metabase's own boot log:
      ```
      db.setup :: Verifying postgres Database Connection ...
      db.setup :: Successfully verified PostgreSQL 16.9 (Debian 16.9-1.pgdg130+1)
                  application database connection. ✅
      liquibase.changelog :: Creating database history table with name: public.databasechangelog
      ```

      Both containers healthy:
      ```
      mb-app        metabase/metabase:v0.54.15   Up (healthy)   0.0.0.0:3000->3000/tcp
      mb-postgres   postgres:16.9                Up (healthy)   0.0.0.0:5433->5432/tcp
      ```

      Proved the vanilla instance really serves the sample dataset, not just that it boots —
      completed setup, logged in, and ran a query end to end:
      ```
      /api/health              -> HTTP 200 {"status":"ok"}
      /api/session (login)     -> HTTP 200, session issued for daksh@deeptune.com
      /api/database            -> id=1 'Sample Database' engine=h2 is_sample=True
      select count(*) from ORDERS -> [[18760]]  status: completed
      ```

      **Issue found and resolved along the way.** The dump hardcodes the sample DB at
      `/app/plugins/sample-database.db`, but this image's `WORKDIR` is `/`, so it actually
      extracts to `/plugins/sample-database.db.mv.db`. I first tried forcing
      `MB_PLUGINS_DIR=/app/plugins` to make the container match the data — that fails:
      the JVM runs as a non-root user and `/app` is root-owned `755`, so Metabase throws
      `java.nio.file.AccessDeniedException: /app/plugins` and silently falls back to a temp
      dir. Reverted to the default `/plugins` (which is `777` in the image, hence writable).
      Metabase is expected to rewrite a moved sample-DB path on startup — **verifying that
      empirically after the restore rather than assuming it.**

- [x] **Step 3 — Restore `metabase_envdata.sql` into the Metabase Postgres DB**

      Sequence: stopped Metabase (so it held no connections) → dropped/recreated
      `metabase_app` (it held the vanilla Step 2 schema, which would have collided) →
      `pg_restore` → verified in `psql` → restarted Metabase → verified through the API.

      **Restored with `pg_restore` exit code 0 and zero errors.**

      A note on *which* `pg_restore`: running the host client (18.4) against the 16.9 server
      completed the data correctly but logged `errors ignored on restore: 1` —
      `ERROR: unrecognized configuration parameter "transaction_timeout"`. That is a
      Postgres 17+ session setting the newer client emits and the 16.9 server rejects;
      harmless, but noisy. Re-ran through the **in-container** `pg_restore` (16.9, matching
      the server exactly) for a genuinely clean restore. Both produced identical row counts.

      Verification — every count matches the dump exactly:
      ```
      tables: 92              collection: 14
      core_user: 12           metabase_table: 8
      report_dashboard: 6     query_execution: 2997
      report_card: 86         databasechangelog: 475 (pre-boot)
      report_dashboardcard: 81
      ```
      `citext 1.6` extension restored. All 92 tables owned by `postgres`.

      All 6 dashboards present, with their cards attached:
      ```
       id |             name              | cards
        1 | E-commerce Insights           |    35
        2 | Revenue Dashboard             |     8
        3 | Product Dashboard             |    13
        4 | Customer Funnel Dashboard     |     6
        5 | Discount Information Dahboard |     8
        6 | Personal Analytics            |    11
      ```
      All 12 users restored (`daksh@`, `tim@`, `john@`, `shaily@`, `chirag@`, `happy@`,
      `heleina@`, `paul@`, `miller@`, `shawn@`, `chris@` + the inactive internal account).

      Metabase then booted clean against the restored DB and **recognized the seeded users**:
      ```
      /api/session/properties  -> has-user-setup: True | version: v0.54.15
      /api/session (login)     -> HTTP 200, session issued for daksh@deeptune.com
      /api/dashboard           -> all 6 seeded dashboards
      /api/collection          -> 13 collections
      ```

      **Seeded questions actually execute against the sample data** — this is the part that
      proves the environment is genuinely usable, not just structurally intact:
      ```
      'Revenue'                     -> completed, 12 rows  ['Created At: Quarter','Sum of Total']
                                       ['2023-07-01', 56686.65] ...
      'Revenue by state'            -> completed, 48 rows  ['State','Sum of Total']
                                       ['AK', 36201.68] ...
      'Revenue by product category' -> completed,  4 rows  6 cols incl. 'Product → Category'
      ```

      **The `/app/plugins` path issue resolved itself, as hoped — now verified, not assumed.**
      Metabase rewrote the sample database's path on boot:
      ```
      metabase.sample-data :: Loading sample database
      util.files :: Extract file /sample-database.db.mv.db -> /plugins/sample-database.db.mv.db
      sync.util   :: STARTING: Sync h2 Database 1 ''Sample Database''
      ```
      and the stored row changed from `file:/app/plugins/sample-database.db` to
      `file:/plugins/sample-database.db`. So no compose workaround is needed.

      **Correction to something I said mid-run.** I first reported "zero schema drift" based
      on the log line `No unrun migrations found ✅`. That was wrong — `databasechangelog`
      went **475 → 480**, so v0.54.15 applied 5 forward changesets:

      | changeset | what it does |
      |---|---|
      | `v52.2025-05-28T00:00:01` | set download-results to `no` where view-data is blocked |
      | `v53.2025-05-06T16:03:19` | support indexing values with bigint ids |
      | `v54.2025-05-13T21:46:53/54/55` | search-index metadata: language, replace index, concurrency guard |

      Consequence checked rather than assumed: the only one that could touch seeded data is
      the v52 permissions migration, and it was a **no-op** here — `data_permissions` is
      byte-identical to the dump (all 5 rows, same values). The other four are search-index
      plumbing. **No seeded content was altered.**

      This also revises where the dump came from: those changesets are dated May 2025 but
      absent from a dump taken 2025-07-09, which means the source instance was running
      **~v0.54.5** and was never upgraded. My pin reasoning ("the patch current on the dump
      date") was therefore off; v0.54.5 is the true match. I'd still keep v0.54.15: the 5
      migrations change no seeded data, they apply identically on every fresh restore so
      resets stay deterministic, and it carries three months of fixes. Say the word if you'd
      rather have a literally zero-migration gym and I'll re-pin to v0.54.5.

      Also worth knowing: ~25 log lines match `/ERROR/` but all are benign — they're Quartz
      scheduler `INFO` lines resetting restored job triggers from `ERROR` state to `WAITING`,
      which is expected since the dump captured those triggers mid-state.

## Open Questions

- None blocking. Both earlier questions are resolved: Colima chosen as the runtime, and
  login credentials received (`daksh@deeptune.com`, recorded in the knowledgebase).
- One decision you may want to weigh in on, not blocking: keep the `v0.54.15` pin
  (5 no-op-for-data migrations) or re-pin to `v0.54.5` for an exact zero-migration restore.

## Next Phase — NOT STARTED

Per the guardrails I'm stopping here. Not created, not touched: `agent/`, `infra/`,
`tasks/`, and `resources/tasks.json`. Awaiting your scope for the agent + rollout work.
`INFRA_KNOWLEDGEBASE.md` §7–8 records open infra questions for that phase (how the agent
authenticates, browser-vs-API, and how state resets between rollouts) — all clearly marked
as unbuilt.

## Commands Reference

```bash
# ---------- Step 1: environment ----------
brew install colima docker docker-compose
mkdir -p ~/.docker
printf '{"cliPluginsExtraDirs":["/opt/homebrew/lib/docker/cli-plugins"]}' > ~/.docker/config.json
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"     # libpq is keg-only
docker --version && docker compose version
docker info --format 'Server: {{.ServerVersion}} | Arch: {{.Architecture}}'

# ---------- Colima VM (does NOT survive reboot) ----------
colima start --cpu 4 --memory 8 --disk 60
colima status

# ---------- Dump inspection (read-only) ----------
head -c 200 resources/metabase_envdata.sql | xxd                     # PGDMP magic bytes
pg_restore --list resources/metabase_envdata.sql | head -40

# ---------- Step 2: boot the gym ----------
cd /Users/gkumar/assessments/hackathon/metabase
docker compose up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done
docker compose ps
curl -s http://localhost:3000/api/health

# verify postgres is the app DB, from the host
PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d metabase_app -c 'select version();' -c '\dt'

# verify the sample dataset is queryable through Metabase
SID=$(curl -s -X POST http://localhost:3000/api/session -H 'Content-Type: application/json' \
      -d '{"username":"daksh@deeptune.com","password":"<password>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -H "X-Metabase-Session: $SID" http://localhost:3000/api/database
curl -s -X POST -H "X-Metabase-Session: $SID" -H 'Content-Type: application/json' \
     http://localhost:3000/api/dataset \
     -d '{"database":1,"type":"native","native":{"query":"select count(*) from ORDERS"}}'

# ---------- Step 3: restore the env data ----------
cd /Users/gkumar/assessments/hackathon

# 1. stop Metabase so it holds no connections to the app DB
docker compose -f metabase/docker-compose.yml stop metabase

# 2. drop + recreate an EMPTY target (the vanilla Step 2 schema would collide)
docker exec mb-postgres psql -U postgres -d postgres \
  -c "DROP DATABASE IF EXISTS metabase_app;" \
  -c "CREATE DATABASE metabase_app OWNER postgres;"

# 3. restore. Prefer the IN-CONTAINER pg_restore: its client version (16.9) matches the
#    server, so no "unrecognized configuration parameter transaction_timeout" noise.
#    resources/ is bind-mounted READ-ONLY at /resources, so the dumps can't be modified.
docker exec mb-postgres pg_restore -U postgres -d metabase_app /resources/metabase_envdata.sql
echo "exit code: $?"      # => 0, zero errors

#    (host-client equivalent from the spec — works, but logs 1 ignored error)
#    pg_restore -d metabase_app -U postgres -h localhost -p 5433 resources/metabase_envdata.sql

# 4. verify the restore
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"; export PGPASSWORD=postgres
psql -h localhost -p 5433 -U postgres -d metabase_app -c '\dt'
psql -h localhost -p 5433 -U postgres -d metabase_app -c \
  "select id, name, (select count(*) from report_dashboardcard dc where dc.dashboard_id=d.id) as cards
   from report_dashboard d order by id;"
psql -h localhost -p 5433 -U postgres -d metabase_app -c \
  "select id, email, is_superuser from core_user order by id;"

# 5. bring Metabase back up against the restored DB
docker compose -f metabase/docker-compose.yml up -d metabase
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done
docker logs mb-app 2>&1 | grep -iE "unrun migrations|Migrations Current|sample database"

# 6. confirm no unexpected schema drift / no seeded-data changes
psql -h localhost -p 5433 -U postgres -d metabase_app -c \
  "select id, comments from databasechangelog where dateexecuted > '2025-07-09' order by orderexecuted;"
psql -h localhost -p 5433 -U postgres -d metabase_app -c "select * from data_permissions order by id;"

# 7. execute a real seeded dashboard card end to end
#    POST /api/dashboard/<dash>/dashcard/<dashcard>/card/<card>/query
curl -s -X POST -H "X-Metabase-Session: $SID" -H 'Content-Type: application/json' \
     http://localhost:3000/api/dashboard/2/dashcard/37/card/9/query -d '{}'
```

> zsh note: `set -- $var` does **not** word-split (unlike bash), so loops that rely on it
> silently pass the whole string as `$1`. Use `${=var}` or write the values explicitly.

---

# Phase 2: Agent Setup — Visual Computer-Use Only

## Status: Agent setup complete — ready to scope the wiring phase

Steps 4–7 all verified with real output, and Step 8's three design decisions are signed off.
No agent loop has been wired — that awaits your explicit go-ahead.

## Completed

- [x] **Step 4 — Python environment**

      **System Python is 3.9.6, which is below the repo's tested range** — its CI matrix is
      `["3.10", "3.11", "3.x"]`. Used Homebrew's Python 3.12.13 instead.

      ```bash
      cd /Users/gkumar/assessments/hackathon/agent
      /opt/homebrew/bin/python3.12 -m venv .venv
      ./.venv/bin/pip install -r computer-use-preview/requirements.txt
      ```

      Read `requirements.txt` before installing anything, as instructed. What it actually
      pulls in — 8 direct dependencies, nothing surprising, no system-level packages:

      | package | pin | why it's there |
      |---|---|---|
      | `google-genai` | `>=2.7.0` | the Gemini SDK; carries the ComputerUse tool types |
      | `playwright` | `==1.55.0` | **the browser harness ships with the repo** |
      | `pydantic` | `==2.12.0` | `EnvState` model (screenshot bytes + url) |
      | `browserbase` | `==1.4.0` | remote-browser alternative — unused, we run local |
      | `termcolor` / `rich` | – | console output only |
      | `python-dotenv` | `==1.0.1` | loads `.env` |
      | `pytest` | – | the repo's own tests |

      Resolved to `google-genai 2.8.0`, `playwright 1.55.0`, `pydantic 2.12.0`.
      Then `./.venv/bin/playwright install chromium` → Chromium 140.0.7339.16.

      Note: the README says `playwright install chrome`, but the code calls
      `self._playwright.chromium.launch(...)` with **no `channel=` argument**, so it uses
      bundled Chromium, not system Chrome. `chromium` is the correct install target.

- [x] **Step 5 — computer-use-preview understood (read-only, no code written)**

      Full write-up in `INFRA_KNOWLEDGEBASE.md` §12. Summary of the four things asked:

      **The action loop.** `agent_loop()` spins `run_one_iteration()` until it returns
      `COMPLETE`. One iteration = send full conversation history → model returns reasoning
      text + zero or more `FunctionCall`s → **zero function calls means the task is done** →
      otherwise each call is dispatched through `handle_action()` onto the `Computer`, which
      returns an `EnvState(screenshot: bytes, url: str)` → that goes back as a
      `FunctionResponse` carrying the PNG inline → repeat. Screenshots from all but the most
      recent 3 turns are stripped from history (`MAX_RECENT_TURN_WITH_SCREENSHOTS = 3`) to
      bound context growth.

      **Action types — two different sets, chosen by model name.** The tool schema is
      declared *server-side* via `types.Tool(computer_use=types.ComputerUse(environment=
      ENVIRONMENT_BROWSER))`, so the model gets the schema from the API, not from local
      declarations. `excluded_predefined_functions` can drop individual actions.
      - Modern (`gemini-3.5-flash` and newer, incl. the default `gemini-3.6-flash`):
        `click`, `double_click`, `triple_click`, `middle_click`, `right_click`, `mouse_down`,
        `mouse_up`, `move`, `type`, `drag_and_drop`, `wait`, `press_key`, `key_down`,
        `key_up`, `hotkey`, `take_screenshot`, `scroll`, `go_back`, `go_forward`, `navigate`
      - Legacy (`gemini-2.5-computer-use-preview-10-2025`, `gemini-3-flash-preview`,
        `gemini-3.1-pro-preview`): `open_web_browser`, `click_at`, `hover_at`, `type_text_at`,
        `scroll_document`, `scroll_at`, `wait_5_seconds`, `go_back`, `go_forward`, `search`,
        `navigate`, `key_combination`, `drag_and_drop`

      **Coordinates are normalized 0–1000**, not pixels. `denormalize_x(x) = int(x / 1000 *
      viewport_width)`. So the model never sees the real resolution and the same policy works
      at any viewport.

      **What it expects as "the screen".** Raw **PNG bytes**, viewport-only
      (`full_page=False`), passed as `types.FunctionResponseBlob(mime_type="image/png")`
      alongside the current URL. **It brings its own harness** — a complete Playwright
      implementation (`computers/playwright/playwright.py`) plus a Browserbase one, both
      behind an abstract `Computer` interface. Default viewport is **1440x900**
      (`PLAYWRIGHT_SCREEN_SIZE` in `main.py`). No resolution is enforced; `screen_size()`
      reads the live viewport, so changing it is safe.

      **Safety/refusal behaviour worth knowing now.** Three mechanisms:
      1. Per-action `safety_decision` in the function-call args. When it's
         `require_confirmation`, the code calls **`input()` and blocks for a human**. In an
         unattended rollout this hangs forever — must be handled before any batch runs.
      2. Prompt-level blocking: `prompt_feedback.block_reason == SAFETY` raises `ValueError`.
      3. `MALFORMED_FUNCTION_CALL` is retried by continuing the loop.

- [x] **Step 7 — browser harness confirmed to see the gym**

      Wrote `agent/verify_harness.py` — screenshot only, **no Gemini, no action-taking**. It
      deliberately drives the repo's own `PlaywrightComputer` rather than a hand-rolled
      Playwright script, since verifying a harness we won't use would prove nothing.

      ```bash
      cd agent && PLAYWRIGHT_HEADLESS=1 ./.venv/bin/python verify_harness.py
      ```
      ```
      url reported       : http://localhost:3000/auth/login?redirect=%2F
      screenshot bytes   : 66644
      PNG magic          : OK (valid PNG)
      PNG dimensions     : 1440x900
      harness screen_size: 1440x900
      match?             : True
      page <title>       : 'Login · Metabase'
      password inputs    : 1
      RESULT: PASS
      ```

      Screenshot saved to `agent/artifacts/step7_login_page.png` and **visually confirmed** —
      it renders the real "Sign in to Metabase" card with email/password fields and the
      Metabase illustration, not a blank or error page. Format and dimensions match Step 5's
      findings exactly (PNG, viewport-only, 1440x900).

      **Gotcha found:** `PlaywrightComputer.navigate()` prepends `https://` to any URL
      lacking a scheme, so `navigate("localhost:3000")` becomes `https://localhost:3000` and
      fails. The gym URL must always be passed as `http://localhost:3000`.

- [x] **Step 6 — Gemini API key confirmed live and correctly scoped**

      Key stored in a project-local `.env` at the repo root, git-ignored via a new
      `.gitignore`. Loaded with `python-dotenv`, which the repo already uses.

      Wrote `agent/verify_gemini.py` — no browser, no screenshots, no Metabase.

      ```bash
      cd agent && ./.venv/bin/python verify_gemini.py
      ```
      ```
      key loaded: AQ.Ab8...O2Gg (len 53)
      USE_VERTEXAI: false

      [1] plain generate_content -> gemini-3.6-flash
          response text : 'OK'
          tokens        : prompt=8 total=90

      [2] generate_content WITH ComputerUse tool -> gemini-3.6-flash
          accepted the ComputerUse tool: True
          response text : 'I have web browsing tools that allow me to navigate web pages,
                           interact with elements through clicking and typing, scroll,
                           press keys, drag and drop, and capture screenshots.'

      [3] model reachability (ComputerUse tool declared on each)
          gemini-3.6-flash                              OK
          gemini-3.5-flash                              OK
          gemini-3.5-flash-lite                         OK
          gemini-3-flash-preview                        OK
          gemini-2.5-computer-use-preview-10-2025       OK

      RESULT: PASS
      reachable computer-use models: 5/5
      ```

      Check 2 matters more than check 1: a plain-text call only proves the key is valid,
      whereas declaring the `ComputerUse` tool proves it's scoped for computer use — and the
      model's own answer confirms it sees the browser action set.

      **A false failure I corrected.** My first reachability probe pinged each model with no
      tool declared, and `gemini-2.5-computer-use-preview-10-2025` returned
      `400 INVALID_ARGUMENT: This model requires the use of the Computer Use tool`. That
      looked like a key-scope problem but was a flaw in my test — that model hard-rejects any
      request without the tool. With the tool declared it works, and it immediately returns a
      `function_call` rather than text, which is the expected shape for a computer-use model.
      Fixed the script to declare the tool on every probe; all 5 models are reachable.

---

## Step 8 — Design decisions ✅ SIGNED OFF

All three answered by you. Recorded here as the contract for the wiring phase. Per the
"discuss, do not build" instruction, **none of this is implemented**.

1. **Auth — DECIDED: use the existing `daksh@deeptune.com` admin credentials.**
   The agent logs in visually with the real credentials. No extra user is seeded, so the
   restored env stays exactly as dumped and the reset flow needs no extra step. Login can
   also be part of a task if a task calls for it.

   Two consequences to design around at wiring time, neither blocking:
   - The password will appear in the model's prompt/context and therefore in every rollout
     transcript. Worth scrubbing at the logging layer rather than leaving it in saved traces.
   - All 12 seeded users are `is_superuser=t`, so the agent has full admin and a wrong click
     can reach destructive settings pages. **The per-rollout reset bounds this** — anything
     it breaks is wiped by the next restore, which is what makes this choice reasonable.

2. **Reset — DECIDED: Postgres `TEMPLATE` optimization, built now rather than later.**
   Restore the dump once into a pristine template DB, then per rollout:
   `DROP DATABASE metabase_app` → `CREATE DATABASE metabase_app TEMPLATE metabase_pristine`.
   Avoids re-running `pg_restore` every rollout.

   Carried-over constraints from Phase 1 that still apply: `colima start` before
   `docker compose up -d` (VM does not survive reboot), and the **in-container**
   `pg_restore` when building the template, to avoid the 18.4-vs-16.9 `transaction_timeout`
   error.

   Honest caveat recorded at decision time: the ~30–60s Metabase JVM restart still dominates
   reset cost, so the template mainly removes the `pg_restore` portion. Two things to verify
   when this is built — Postgres refuses `CREATE DATABASE ... TEMPLATE` while any session is
   connected to the template, and Metabase must be fully stopped so it isn't holding a
   connection to the database being dropped.

3. **Harness lifecycle — DECIDED: a fresh browser per attempt.**
   Strongest isolation: cookies, localStorage and the Metabase session all start clean, so
   no rollout can inherit hidden state from the previous one. Requires **zero changes** to
   the repo's `PlaywrightComputer`, which already launches a browser in `__enter__` and
   closes it in `__exit__` — one `with PlaywrightComputer(...)` block per attempt is exactly
   this. The ~1–3s launch cost is negligible next to the Metabase restart.

## Addendum — first agent-loop run (gate lifted for a one-off read-only demo)

You asked to see a running example and approved a read-only run. This used the **upstream
`main.py` unchanged** — no agent code was written by me, so the guardrail on *wiring* the
loop still stands.

Task given: log in as `daksh@deeptune.com`, navigate to the dashboard list, report the
dashboard names, change nothing.

**Bug found and fixed — upstream's own `.env.example` breaks the Gemini API path.**
`agent.py` constructs its client unconditionally:

```python
self._client = genai.Client(
    api_key=os.environ.get("GEMINI_API_KEY"),
    vertexai=os.environ.get("USE_VERTEXAI", "0").lower() in ["true", "1"],
    project=os.environ.get("VERTEXAI_PROJECT"),
    location=os.environ.get("VERTEXAI_LOCATION"),
)
```

It passes `project`/`location` even when `USE_VERTEXAI=false`, and google-genai rejects that:

```
ValueError: Project/location and API key are mutually exclusive in the client initializer.
```

`.env.example` ships `VERTEXAI_LOCATION=us-central1`, so copying it verbatim and adding an
API key fails instantly. **Fix: `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` must be empty**
whenever `GEMINI_API_KEY` is used. Our `.env` now has both blank with a comment explaining
why they must stay that way.

**Precise accounting of what is and isn't verified** — worth being exact here:

| claim | evidence |
|---|---|
| Headed Chromium launches and loads the gym | **Captured.** Run 1 printed `Creating session...` / `Started local playwright.` — `PlaywrightComputer.__enter__` had already launched the browser and navigated to `http://localhost:3000` before the crash. |
| Harness screenshots the gym at the right format/size | **Captured** — Step 7, `RESULT: PASS`, image reviewed. |
| The key reaches computer-use models | **Captured** — Step 6, 5/5 reachable. |
| A full Gemini-driven action loop against Metabase | **Observed by you, not captured by me.** Run 1 crashed at client init *before* any Gemini call. The re-run after the `.env` fix was interrupted before I executed it, so I hold no terminal log of a complete loop. |

So the end-to-end loop is not yet evidenced by an artifact on my side. Happy to do one clean
captured run whenever you want the log — the fix is already in place.

**Gym verified unmodified after the run:** `report_dashboard: 6`, `report_card: 86`,
`collection: 14`, `core_user: 12` — identical to the restored dump. The read-only
instruction held, which matters because no reset flow exists yet.

**Run closed.** No `main.py`, Chromium, or `headless_shell` processes remain.

## Open Questions

- None blocking. All three Step 8 questions are signed off above.
- Optional: one clean captured agent-loop run, to have the log as an artifact.

## Commands Reference — Phase 2

```bash
# ---- Step 4: python env ----
cd /Users/gkumar/assessments/hackathon/agent
git clone --depth 1 https://github.com/google-gemini/computer-use-preview.git
/opt/homebrew/bin/python3.12 -m venv .venv          # NOT system python 3.9.6 (too old)
./.venv/bin/pip install -r computer-use-preview/requirements.txt
./.venv/bin/playwright install chromium             # NOT `chrome` — code launches bundled chromium

# ---- Step 7: harness verification (no Gemini involved) ----
cd /Users/gkumar/assessments/hackathon/agent
PLAYWRIGHT_HEADLESS=1 ./.venv/bin/python verify_harness.py
open artifacts/step7_login_page.png

# ---- Step 6: Gemini key check (no browser, no Metabase) ----
cd /Users/gkumar/assessments/hackathon/agent
./.venv/bin/python verify_gemini.py

# ---- Demo: upstream agent loop against the gym (gated — ask before running) ----
cd /Users/gkumar/assessments/hackathon/agent/computer-use-preview
../.venv/bin/python main.py \
  --query "<task>" --env playwright \
  --initial_url "http://localhost:3000" \
  --highlight_mouse
# NOTE: --initial_url MUST include http:// — navigate() prepends https:// otherwise.
# NOTE: PLAYWRIGHT_HEADLESS must be empty/unset for a headed window (truthiness trap).

# ---- gym must be up first (Phase 1) ----
colima start                                        # VM does not survive reboot
cd /Users/gkumar/assessments/hackathon/metabase && docker compose up -d
```

---

# Phase 3: Rollout Pipeline — Milestone 1

## Status: ✅ Milestone 1 complete — one attempt runs, is recorded, and is graded

`problem10` runs end to end and grades **COMPLETED_CORRECT / reward 1**, driven entirely
visually. 44 tests pass. Stopping here for your review before the job runner and UI.

### What was built

| File | Role |
|---|---|
| `rlgym/schemas.py` | Artifact models + the status→reward contract |
| `rlgym/tasks.py` | `tasks.json` loader (answers are double-encoded JSON strings) |
| `rlgym/computer.py` | `GymComputer`: pre-auth, video, domain lock, telemetry |
| `rlgym/capture_auth.py` | Mints `storage_state.json` once |
| `rlgym/agents/base.py` | `Agent` protocol — the agent-agnostic seam |
| `rlgym/agents/gemini_cu.py` | Gemini computer-use policy + `submit_answer` |
| `rlgym/executor.py` | One attempt in a subprocess → `AttemptArtifact` |
| `rlgym/grader.py` | Versioned reward function |
| `rlgym/run_one.py` | Demo / debug CLI |
| `tests/` | 44 tests |

### Verified behaviours (real output, not assumed)

```
problem10, 40 steps          -> COMPLETED_CORRECT  reward 1     verdict pass
  expected {'avg_rating': 3.15}   actual {'avg_rating': 3.15}
  39 steps, 273s, 518k tokens, used_sql_editor=True

invalid GEMINI_API_KEY       -> ERRORED            reward null  not_graded
  carries the real 401 UNAUTHENTICATED

expired session cookie       -> ERRORED            reward null  not_graded
  "not authenticated after injecting storage_state ... re-run capture_auth"

--max-steps 1                -> MAX_STEPS_EXCEEDED reward 0
```

The middle two matter most: under upstream's loop both would have been reported as
`COMPLETE` and graded **reward 0**, silently mislabelling infra failures as agent failures.

### The most interesting finding: budget blindness

The first full `problem10` run **failed at 40/40 steps** — and the diagnosis was worth more
than the pass that followed. The agent had the correct value on screen at **step 12**:

> `SELECT AVG("RATING") FROM "PUBLIC"."PRODUCTS" WHERE "CATEGORY" = 'Widget'` → **3.15**

It then spent 28 steps in a verification spiral. From its own reasoning at step 13:

> *"the initial calculation gave me 3.15. But I can't just leave it at that, not without due
> diligence... I need to be absolutely certain."*

and at step 19, having computed `SUM=170.3, COUNT=54`:

> *"170.3 / 54 gives me 3.1537037... I need to cross-reference my manual calculation with
> what Metabase itself is displaying."*

It could not decide whether Metabase's `3.15` was rounded or exact, and never submitted.
**Either value would have graded pass** — the grader's `abs_tol=1e-2` covers the 0.0037 gap.
The only failure was not submitting, because the agent had no way to observe its budget
draining.

Fix: observations now carry `steps_remaining`, plus an explicit submit instruction in the
final 8 steps. The same task then passed. Standard practice in agent harnesses, and clearly
the right call — the model was being asked to manage a resource it couldn't see.

**Side benefit:** this incidentally confirms `tasks.json`'s ground truth is valid in our
image for `problem10` — the gym really does produce 3.15. That was the risk we agreed to stop
worrying about, and at least one data point says it's fine.

### One design correction found by a test

`search` was initially repointed at the gym URL, making it a silent redirect home. A test
caught it. The documented design is *refusal*, so the code changed rather than the test:
`search` keeps its off-origin target and the domain lock refuses it, giving the agent an
observable boundary instead of a search that appears to return nothing.

### How to test it yourself

```bash
colima start
cd metabase && docker compose up -d && cd ..
until [ "$(docker inspect -f '{{.State.Health.Status}}' mb-app)" = "healthy" ]; do sleep 3; done

agent/.venv/bin/python -m rlgym.capture_auth        # once
agent/.venv/bin/python -m pytest tests/ -q          # 44 passed

agent/.venv/bin/python -m rlgym.run_one --task problem10            # ~4-5 min, expect pass
agent/.venv/bin/python -m rlgym.run_one --task problem1 --headed    # watch it work
```

Artifacts land in `runs/adhoc/<attempt_id>/` — `artifact.json`, `evaluation.json`,
`steps/*.png`, `video/*.webm`, `worker.log`.

### Not built yet

Job runner (T×K, parallel, resumable), FastAPI + React UI, reset-between-rollouts
(unnecessary today — all 10 tasks are read-only), and the Milestone 3 write-up.
