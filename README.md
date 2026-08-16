# Debate Tracker v2

FastAPI + SQLModel backend for tracking debate tournaments. Two independent formats are
supported, selected per-tournament via `Tournament.format`:

- **Two-team** (Asian/Australs/WSDC, prop vs. opp): `/api/v1/rounds/{id}/debates`,
  `/api/v1/debates/{id}/ballots`, and the `/api/v1/tournaments/{id}/speaker-tab`,
  `team-standings`, `summary`, `side-stats`, `motion-stats` stats routes.
- **BP** (British Parliamentary, 4 teams — OG/OO/CG/CO): `/api/v1/rounds/{id}/bp-debates`,
  `/api/v1/bp-debates/{id}/ballots`, and the equivalent `/api/v1/tournaments/{id}/bp/...`
  stats routes. A BP result is a ranking of the four teams (3/2/1/0 team points), not a
  win/loss, so there's no `winner` field and no win-rate stats for BP. Elimination rounds
  that report only advance/eliminate (no full ranking — common from quarterfinals
  onward) get an `advanced` flag per team instead, visible on `bp-debates/{id}/result`
  and rolled up into `team-standings`' `elim_advances`/`elim_eliminations`.

Career profiles (`/api/v1/debaters/{id}/profile`, `/api/v1/judges/{id}/profile`) are
two-team-only for now; `POST /api/v1/stats/refresh` reports how many BP tournaments were
excluded via `bp_tournaments_excluded`.

Backed by Postgres (`DATABASE_URL` in `.env`); falls back to in-memory SQLite if unset.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
createdb debate_tracker_v2   # once, if it doesn't exist yet
```

Set `DATABASE_URL` in `.env`, e.g.:

```
DATABASE_URL=postgresql+psycopg://<user>@localhost:5432/debate_tracker_v2
```

## Configuration

All settings are read from `.env` via `pydantic-settings` (`app/core/config.py`). A
typo'd variable name is silently ignored rather than erroring, so double-check spelling.

| Variable              | Default    | Purpose                                                         |
|------------------------|------------|------------------------------------------------------------------|
| `DATABASE_URL`         | in-memory SQLite | Postgres (or other SQLAlchemy) connection string.           |
| `SQL_ECHO`             | `false`    | Log every SQL statement SQLAlchemy executes.                     |
| `SEED_ON_STARTUP`      | `false`    | Run the sample-data seed automatically when the app starts.      |
| `LOG_LEVEL`            | `DEBUG`    | Root logging level.                                               |
| `TABBYCAT_BASE_URL`    | unset      | Default Tabbycat instance URL, so it doesn't need to be passed per import request. |
| `TABBYCAT_API_KEY`     | unset      | Default Tabbycat API token, paired with `TABBYCAT_BASE_URL`.     |
| `TABBYCAT_CACHE_DIR`   | unset      | Directory for caching Tabbycat API responses. See [caching](#caching-tabbycat-responses) below. |
| `IMPORT_TRACE`         | `false`    | Verbose per-record tracing during a tournament import.            |

## Run

```bash
make dev
```

Runs under gunicorn rather than bare `uvicorn --reload`: gunicorn's arbiter monitors and
respawns workers that die, and `-w 2` means one worker crashing doesn't take down the
whole listener while it's respawned. Plain `uvicorn --reload` only restarts on a file
change — if the worker process crashes for any other reason, the reload supervisor
doesn't notice, and it keeps the listening socket open with nothing behind it, so new
connections hang forever instead of failing.

- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

## Test

```bash
make test
```

## Database management

```bash
make db-create    # create tables (SQLModel.metadata.create_all)
make db-seed      # load sample data (app/db/seed.py)
make db-drop      # drop all tables — prompts for confirmation
make db-truncate  # delete all rows, keep schema — prompts for confirmation
make db-reset     # drop + create + seed — prompts for confirmation
```

`db-drop`, `db-truncate`, and `db-reset` are destructive and require typing `yes` at a
prompt before they run. All targets shell out to `scripts/db.py` against whatever
`DATABASE_URL` is configured.

## Importing a Tabbycat tournament

Real tournaments can be imported from a [Tabbycat](https://tabbycat.readthedocs.io) instance:

```bash
curl -X POST localhost:8000/api/v1/tournaments/import \
  -H 'Content-Type: application/json' \
  -d '{"base_url":"https://<instance>.calicotab.com","slug":"<slug>","include_ballots":true}'
```

`api_key` can be passed in the request body, or set via the `TABBYCAT_API_KEY` env var
(paired with `TABBYCAT_BASE_URL`) to avoid passing it every time. Both are read by
`pydantic-settings` from `.env`, so a typo'd variable name is silently ignored.

The importer detects two-team vs. BP automatically (by reading the tournament's
`debate_rules__teams_in_debate` preference, or by inferring from the first round's
pairing size if that isn't reachable) and populates the matching schema — check the
response's `format` field. Tabbycat's model is still richer than either local schema
(e.g. multiple motions per round, arbitrary score criteria), so anything that doesn't
map is skipped rather than aborting the import — check the response's `skipped` list.
Importing is a one-shot operation: there's no `tabbycat_id` column to make it
idempotent, so re-importing the same slug raises a 409.

### Caching Tabbycat responses

Set `TABBYCAT_CACHE_DIR` in `.env` to cache every upstream GET response to disk, keyed
by URL under a per-slug subfolder:

```
TABBYCAT_CACHE_DIR=.cache/tabbycat
```

With it set, re-running an import for the same slug (e.g. after `make db-reset`) replays
from disk instead of re-hitting the Tabbycat instance — useful for iterating on the
importer locally without risking the upstream site's rate limits.

- **Off**: leave `TABBYCAT_CACHE_DIR` unset (the default) — every import hits Tabbycat directly.
- **On**: set `TABBYCAT_CACHE_DIR` to a directory path and restart the app.
- **Force a fresh pull**: delete the cache dir, or just the tournament's slug subfolder
  inside it, then re-run the import.
