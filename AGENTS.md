# AGENTS.md

FastAPI + SQLModel backend for tracking debate tournaments. Supports two independent
formats, selected per-tournament via `Tournament.format`:

- **Two-team** (Asian/Australs/WSDC, prop vs. opp) — the original stack: `Debate`,
  `Ballot`, `SpeakerScore`, `app/services/stats/tournament.py`, `app/api/v1/endpoints/
  debates.py` and `ballots.py`.
- **BP** (British Parliamentary, 4 teams — OG/OO/CG/CO, 2 speakers each, no reply, no
  winner) — a parallel stack: `BPDebate`/`BPBallot`/`BPSpeakerScore` models,
  `app/services/stats/bp.py`, `app/api/v1/endpoints/bp_debates.py` and `bp_ballots.py`.
  A BP debate's result is a *ranking* of the four teams worth 3/2/1/0 team points
  (`bp_points_for_rank`), not a win/loss. Elimination rounds sometimes carry no
  points/rank at all upstream — only a win/advance flag per team (2 advance + 2
  eliminated in a quarter/semi, 1 champion + 3 non-champions in the grand final) — in
  which case `BPBallotTeam.advanced`/`BPDebateTeam.advanced` is set instead and
  `rank`/`points` stay `None`, rather than fabricating a placement that isn't there.

Participant-level entities (`Tournament`, `Round`, `Motion`, `Institution`, `Team`,
`TeamMember`, `Debater`, `Judge`) are shared between both formats; the fork happens at
the debate/ballot/score layer. Career profiles (`DebaterProfile`/`JudgeProfile`,
`app/services/stats/profiles.py`) are two-team-only for now — BP tournaments are
explicitly excluded and counted via `RefreshResult.bp_tournaments_excluded`, since
unifying career stats across formats is a deliberate follow-up.

Postgres-backed via `DATABASE_URL` in `.env`; falls back to in-memory SQLite
(`sqlite://`) if unset.

## Setup & running

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make dev
# http://localhost:8000/docs, /health
```

`make dev` and `make test` wrap the `.venv/bin/gunicorn` / `.venv/bin/pytest` commands
below (see `Makefile`). Always use the binaries in `.venv/bin/` directly when not going
through `make` (e.g. `.venv/bin/pytest`, `.venv/bin/gunicorn`) rather than assuming an
activated shell.

Run under gunicorn, not bare `uvicorn --reload`: uvicorn's own `--reload` supervisor only
restarts the worker on a file change — if the worker crashes for any other reason (e.g.
an unhandled exception under concurrent load), it doesn't notice, and the listening
socket stays open with nothing behind it, so new requests hang forever. gunicorn's
arbiter monitors and respawns crashed workers regardless of cause, and `-w 2` keeps one
worker's crash from taking down the whole listener while it respawns.

`app/db/session.py` only uses `StaticPool` + `check_same_thread=False` for the
in-memory `sqlite://` case (required there since every connection must share the one
in-memory DB). Postgres uses SQLAlchemy's normal per-thread pool checkout + 
`pool_pre_ping=True` — do not add `StaticPool` there, it would force every request
thread onto one shared connection.

## Tests

```bash
make test
```

Run the full suite after any model, endpoint, or service change. Tests spin up a fresh
in-memory SQLite engine per test via the `session`/`client` fixtures in
`tests/conftest.py` and override the `get_session` dependency — don't hit the real
`app/db/session.py` engine from tests.

## Layout

- `app/models/` — SQLModel table models. Each file typically defines the table model
  plus `*Create` / `*Public` / `*Update` variants (see `app/models/debate.py`). Relationships
  use string-quoted forward refs and `TYPE_CHECKING` imports to avoid circular imports.
- `app/api/v1/endpoints/` — one router module per resource; registered in
  `app/api/v1/router.py`. Endpoints use `SessionDep` and `get_or_404` from `app/api/deps.py`.
  Uniqueness conflicts are caught as `IntegrityError` and turned into `409`.
- `app/services/stats/` — pure computation over ballots/debates for tournament, debater,
  and judge statistics.
- `app/services/tabbycat.py` — importer that pulls a tournament from a
  [Tabbycat](https://tabbycat.readthedocs.io) instance. Detects two-team vs. BP by
  reading the tournament's `debate_rules__teams_in_debate` preference (readable
  anonymously, no API key), falling back to inferring from the first round's pairing
  size if that endpoint isn't reachable. Tabbycat's model is still richer than either
  local schema (multiple motions per round, arbitrary scoring criteria), so anything
  that doesn't map is skipped, not aborted — see the `skipped` list in `ImportReport`.
  There is no `tabbycat_id` column, so re-importing the same slug is not idempotent and
  raises `409`.
- `app/core/config.py` — `pydantic-settings` config read from `.env` with `extra="ignore"`,
  so a typo'd env var name is silently ignored rather than erroring.

## Conventions

- Python 3.14, full type hints, `X | None` union syntax (not `Optional`).
- Keep model/schema/endpoint changes consistent: adding a field to a table model usually
  means updating its `*Create`/`*Public`/`*Update` counterparts too.
- Match existing router error-handling patterns (`get_or_404`, catching `IntegrityError`
  for conflicts) rather than introducing new patterns.
- No linter/formatter is configured in this repo — match surrounding style by hand.
