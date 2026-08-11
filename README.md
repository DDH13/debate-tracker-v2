# Debate Tracker v2

FastAPI + SQLModel backend for tracking debate tournaments (Asian/Australs/WSDC two-team
format). Backed by an in-memory SQLite database for now.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/uvicorn app.main:app --reload
```

- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

## Test

```bash
.venv/bin/pytest
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
