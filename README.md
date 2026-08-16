# Debate Tracker v2

FastAPI + SQLModel backend for tracking debate tournaments (Asian/Australs/WSDC two-team
format). Backed by Postgres (`DATABASE_URL` in `.env`); falls back to in-memory SQLite
if unset.

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

Tabbycat's model is richer than this app's two-team (prop/opp) schema (e.g. BP-style
4-team debates, multiple motions per round, arbitrary score criteria), so anything that
doesn't map is skipped rather than aborting the import — check the response's `skipped`
list. Importing is a one-shot operation: there's no `tabbycat_id` column to make it
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
