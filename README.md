# GridCoach

An AI training coach that lives **inside your Google Sheet**. Your Strava history, weather, training plan, sleep/life notes and a rolling health snapshot all land in tabs you own; an OpenAI-powered coach in the sidebar reads them, plots your log against your plan, and writes plans back — deterministically, into fixed columns.

> Rationale: people want more control over how they train, and training has become a hybrid-sport journey. A spreadsheet is the most controllable, hackable training log there is — GridCoach just makes it talk back.

## Architecture

```mermaid
flowchart LR
  subgraph Sheet["Google Sheet (source of truth)"]
    PLAN[Training Plan]
    LOG[Training Log]
    HEALTH[Health Notes]
    WX[Weather]
    SNAP[Health Snapshot]
    COACH[Coach Notes]
    SET[Settings]
    SIDEBAR[[Sidebar chat<br/>Apps Script]]
  end

  SIDEBAR -- "UrlFetchApp + X-API-Key" --> API

  subgraph Backend["FastAPI orchestrator"]
    API[/api/chat, /api/strava/sync, …]
    AGENT[OpenAI Responses loop<br/>function tools]
    PIPE[Deterministic pipeline<br/>sync · enrich · snapshot · reconcile]
    DB[(SQLite: tokens,<br/>thread ids)]
  end

  API --> AGENT --> PIPE
  AGENT -- "Sheets API (service account)" --> Sheet
  PIPE --> Sheet
  STRAVA[(Strava API)] -- "OAuth · activities · streams" --> PIPE
  STRAVA -- "webhook POST" --> API
  METEO[(Open-Meteo)] -- "historical hourly · forecast" --> PIPE
```

**What's deterministic (plain Python):** Strava → log rows, weather enrichment + heat-adjusted pace, the Health Snapshot (7d/28d load, ACWR, sport mix, adherence, race countdown), and plan-vs-log reconciliation (the `Status` column on the plan).

**What's agentic (OpenAI):** the sidebar coach — reads any tab, explains what it sees, parses pasted plans into structured rows (`write_training_plan`), adjusts plans for life context (sleep, busy weeks, weather), builds real embedded charts (`create_chart`), and logs observations to Coach Notes. A silent *Background Analyst* run writes one Coach Note after each webhook-delivered activity.

**Anti-hallucination design:** the model never eyeballs a 2-D array for facts. `query_log` answers "last run", "how many", "by month" and "what did I do on <date>" deterministically (filters + aggregates, with sheet row numbers); `read_range` returns header-keyed records (missing key = empty cell); rows are appended as header-keyed objects so columns can't misalign; Strava's `trainer` flag lands in an `Indoor` column so "outdoor" is a filter, not a guess. When challenged, the coach is instructed to re-query and quote the record rather than agree.

State lives in three places: the **Health Snapshot** tab (athlete-visible rolling metrics), a per-spreadsheet `previous_response_id` in SQLite that keeps the OpenAI conversation thread alive across sidebar sessions, and **durable athlete memory** (`remember`/`forget` tools, SQLite, injected into every turn, mirrored to an optional `Coach Memory` tab).

## Orchestration

- **Background jobs with live progress.** Every chat turn and Strava sync runs as a job (`POST /api/chat {background:true}` → `job_id`; `GET /api/jobs/{id}?after=N`). The sidebar polls and renders each step as it happens ("Querying log (Run, outdoor)… ✓ 12 matches", "Running analysis (pandas)… ✓"). No request is bound by Apps Script's 6-minute cap.
- **Confirm-before-destructive.** `delete_rows`, replacing an existing plan, and overwriting non-empty cells are parked as *pending actions*. The coach explains what would change; the sidebar shows Confirm/Cancel; `POST /api/actions/resolve` executes (or discards) it and the coach wraps up. Nothing destructive runs on the model's say-so alone.
- **Nuanced analysis via `analyze_data`.** For questions fixed aggregates can't answer (hot-vs-cool pace, weekly trends, "runs after < 6 h sleep"), the model writes a few lines of pandas; the backend runs them in an isolated `python -I` subprocess (AST-whitelisted imports, no files/network, 30 s cap) over the tabs as DataFrames (numbers coerced, dates parsed, `m:ss` paces as decimals, sheet `row` numbers). Errors return the traceback so the model fixes its code instead of guessing.
- **Strava data layer (separate from the sheet).** The full activity history is cached locally in SQLite (backfilled on first use ≈ 1 API call per 200 activities, incremental afterwards, kept current by the webhook). `strava_stats` (Strava's official YTD / 4-week / all-time per-sport totals), `strava_query` (same filter/aggregate engine as `query_log`, over all years), `strava_zones`, `strava_gear`, and a `strava` DataFrame in `analyze_data` answer history questions **without touching the sheet**. The Training Log stays curated: `sync_strava` writes only the window the athlete asked for, from the cache.
- **Persistent transcript + job resume.** Every turn is stored server-side (`GET /api/history`); reopening the sidebar replays it, re-attaches to a job still running, and re-shows unresolved Confirm cards. Polls retry through tunnel hiccups instead of abandoning a running job.
- **Server-side data movement.** `sort_tab`, `copy_range`, `delete_rows`, `create_chart` — the model never re-types rows through its own output.
- **Model.** Default `gpt-5.5` with `OPENAI_REASONING_EFFORT=medium` (background analyst and confirmation wrap-ups use `low`). `gpt-4.1` works but stops after single tool calls and confabulates rationale on long tasks; `gpt-5.4-mini` is a good cheap/fast alternative.

## Repo layout

```
backend/            FastAPI app (uv project, Python 3.11+)
  app/agent/        prompts, tool schemas, Responses-API orchestrator
  app/services/     sheets (gspread), strava, weather (Open-Meteo), analytics (pure), pipeline, state (sqlite)
  app/routers/      /api/* (sidebar), /auth/strava, /webhook/strava
  scripts/          strava_subscribe.py — manage the webhook subscription
  tests/            analytics + orchestrator loop tests (no network)
apps-script/        Code.gs + Sidebar.html + appsscript.json (paste into the sheet's bound script)
```

## Setup

### 1. Google service account (lets the backend read/write the sheet)
1. Google Cloud console → create/pick a project → enable **Google Sheets API** and **Google Drive API**.
2. IAM → Service Accounts → create one → Keys → **Add key (JSON)** → save as `service_account.json` in the repo root.
3. Share your Google Sheet with the service account's `client_email` as **Editor**.

### 2. Strava API app
1. https://www.strava.com/settings/api → create an app.
2. **Authorization Callback Domain** = the host of your backend (`localhost` for local dev; your ngrok host when tunnelling).
3. Note the Client ID / Secret.

### 3. Backend
```bash
cp .env.example .env          # fill in OPENAI_API_KEY, GRIDCOACH_API_KEY, STRAVA_*, GOOGLE_SERVICE_ACCOUNT_FILE
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000
```
Check: `curl localhost:8000/health`.

The sidebar runs on Google's servers, so the backend must be reachable from the internet (a `localhost` URL in Apps Script fails with "DNS error"). Any tunnel works; cloudflared needs no account:
```bash
# macOS arm64 binary (brew may have no bottle for your OS); other platforms: https://github.com/cloudflare/cloudflared/releases
curl -sSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz | tar xz -C ~/.local/bin
cloudflared tunnel --url http://localhost:8000     # prints https://<random>.trycloudflare.com
```
Then set `PUBLIC_BASE_URL=https://<random>.trycloudflare.com` in `.env`, restart the backend, and put the same URL in the Strava app's **Authorization Callback Domain** (host only, no `https://`). The URL changes each time the tunnel restarts — ngrok with a static domain avoids that.

### 4. Apps Script (the sidebar)
1. Open your sheet → **Extensions → Apps Script**.
2. Replace `Code.gs` with `apps-script/Code.gs`; add an HTML file named `Sidebar` with `apps-script/Sidebar.html`; (optional) enable *Show "appsscript.json"* in project settings and paste `apps-script/appsscript.json`.
3. Save, reload the sheet → a **GridCoach** menu appears.
4. **GridCoach → Configure backend…** → enter your ngrok URL and the `GRIDCOACH_API_KEY` value.
5. **GridCoach → Open coach** → click **Tabs** and tick only the tabs you want (Training Log is the one Strava sync needs) → **Connect Strava** → **Sync Strava**.

### 5. Strava webhook (optional — auto-append + background coach notes)
```bash
cd backend
uv run python scripts/strava_subscribe.py create     # PUBLIC_BASE_URL must be the public ngrok URL
uv run python scripts/strava_subscribe.py list
```
Strava calls `GET /webhook/strava` with `hub.challenge` to verify, then POSTs events. The receiver answers `200` immediately and processes in the background: fetch → weather → upsert log → recompute snapshot → analyst writes a Coach Note.

## Using it

- **"How am I doing?"** → reads Health Snapshot + log; explains ACWR, load, adherence.
- **Paste a plan** (prose, bullets, a coach's table) → parsed into `Training Plan` rows with real dates.
- **"Plan my half marathon"** → reads Settings (race), snapshot (chronic load), Health Notes; writes a periodised plan, keeps your other sports.
- **"I slept 5 hours, big work week"** → appended to Health Notes; coach proposes swaps and applies them if you say so.
- **"When should I do my long run?"** → Weather tab / forecast tool.
- **Select cells and ask "what's this?"** → the selection is passed as context.
- **Analyze last run** → km splits, aerobic decoupling, cardiac drift, cadence, and the weather at the start point.

Every write the agent makes is listed under its reply (`✓ write_training_plan → Training Plan!A2:J44`) and the sheet jumps to that range.

## Sheet tabs — all optional

You decide what lives in your sheet. Nothing is created unless you tick it (sidebar → **Tabs**) or ask the coach to (`create_tab`, which it only calls when you asked for a feature that needs it). Everything degrades gracefully: the snapshot metrics are still computed and available in chat without a Health Snapshot tab; plan reconciliation only runs if you keep a Training Plan; coach notes are skipped without a Coach Notes tab. Bring your own tabs and columns too — the coach reads headers and adapts.

| Tab | Written by | Purpose |
|---|---|---|
| Training Plan | agent (`write_training_plan`), reconciliation fills `Status` | Structured sessions with dates, targets, intensity |
| Training Log | Strava sync (+ weather) — your `Notes` column is never overwritten | One row per activity, heat-adjusted pace |
| Health Notes | you / agent (`append_rows`) | Sleep, RHR, energy, soreness, stress, life notes |
| Weather | `refresh_weather_tab` | 7-day forecast for home location + training tip |
| Health Snapshot | recomputed after every sync | 7d/28d volume, ACWR, sport mix, adherence, race countdown |
| Coach Notes | agent | Dated observations (sidebar + webhook analyst) |
| Settings | you / agent | Race name/date/distance, goal, home lat/lng |

## Tests
```bash
cd backend && uv run pytest
```

## Notes / limits
- Strava standard tier: 100 requests / 15 min, 1000 / day, ≤ 10 connected athletes. Sync uses 1–2 calls per 200 activities; deep-dive uses 2 per activity.
- Heat adjustment is a heuristic (≈1 %/°C apparent temp above 15 °C, capped 25 %) — a fairness lens, not physiology.
- Set `OPENAI_MODEL` to switch models; reasoning models get `reasoning.effort=low` to keep sidebar latency down.
- Apps Script's `google.script.run` has a 6-minute cap; long syncs should use the dedicated **Sync Strava** button rather than chat.
