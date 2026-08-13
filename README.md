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
