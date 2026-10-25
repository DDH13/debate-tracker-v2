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
| `CORS_ORIGINS`         | `["*"]`    | Allowed CORS origins, as a JSON list (e.g. `["https://admin.example.com"]`). |
| `SESSION_COOKIE_NAME`  | `dt_session` | Name of the admin session cookie.                              |
| `SESSION_TTL_DAYS`     | `14`       | How long an admin sign-in lasts.                                   |
| `SESSION_COOKIE_SECURE` | `false`   | Mark the session cookie `Secure`. Set `true` in production (HTTPS). |
| `SESSION_COOKIE_SAMESITE` | `lax`   | `lax` / `strict` / `none`. See [authentication](#authentication). |

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

## Authentication

Reads (`GET`) on the tournament/people/stats endpoints are public. Every write on them
(imports, merges, `/stats/refresh`, CRUD) and everything under `/api/v1/admin` needs a
signed-in **board** user; `volunteer` users can sign in but get `403` there.

```bash
make user EMAIL=chair@example.com NAME="Chair"                  # board user, prompts for password
make user EMAIL=vol@example.com NAME="Volunteer" ROLE=volunteer
```

| Endpoint | Purpose |
|---|---|
| `POST /auth/login` | `{email, password}` → sets an httpOnly session cookie, returns the user. |
| `POST /auth/logout` | Revokes the session and clears the cookie. |
| `GET /auth/me` | The signed-in user, or `401`. |
| `/admin/users` | Board-only user management: list/create, `PATCH` (name, email, role, `is_active`), `POST /admin/users/{id}/password`. The last active board user can't be demoted or deactivated. |

Sessions are server-side rows (`authsession`, only the token's sha256 is stored), so
deactivating a user or resetting their password signs them out immediately.

The cookie defaults assume the UI and API share an origin (the UI's Vite dev proxy, or a
reverse proxy in production). If they're ever split across sites, set
`SESSION_COOKIE_SAMESITE=none`, `SESSION_COOKIE_SECURE=true`, an explicit `CORS_ORIGINS`
list, and add `allow_credentials=True` to the CORS middleware in `app/main.py`.

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

### Identity matching

Names are normalized before matching (HTML stripped, Unicode/quotes/dashes/whitespace
cleaned; all-lower or all-upper person names are title-cased, mixed case is kept):

- **Institutions** match an existing row when their key (lowercase, letters and digits only)
  is equal, so "St. Peter's College" reuses "St Peters College". Fuzzy similarity is never
  used to auto-match — only the `/institutions/similar` search uses it.
- **People** match by email, else by case-insensitive full name *and* institution. When a
  person is created while the same name already exists under another institution, the
  `skipped` list gets a "possible duplicate of #12 (Royal College), ..." note — review those
  with the merge tooling below.
- Motion text and info slides are stripped of HTML.

The report's `resolutions` list shows, for every upstream institution, debater and judge,
which local row it resolved to and how (`created`, `email`, `name+institution`,
`institution-key`).

### Previewing an import

`POST /api/v1/tournaments/import/preview` takes the same body as `/import`, runs the whole
import, then **rolls it back**, returning the normal report (counts, `skipped`,
`resolutions`) without writing anything. With `TABBYCAT_CACHE_DIR` set, a following real
import replays from the cache, so previewing costs no extra upstream calls.

```bash
curl -X POST localhost:8000/api/v1/tournaments/import/preview \
  -H 'Content-Type: application/json' \
  -d '{"base_url":"https://<instance>.calicotab.com","slug":"<slug>"}'
```

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

## Merging duplicates

Imports can leave duplicate people and institutions. These endpoints find and fold them
together; each merge is a single transaction, so a failure changes nothing.

| Endpoint | Purpose |
|----------|---------|
| `GET /debaters/duplicates?by=name\|name_birthdate` | Groups of debaters sharing a (case-insensitive) name, optionally also a birthdate. |
| `GET /judges/duplicates` | Same, for judges. |
| `GET /institutions/similar?name=&threshold=0.8` | Institutions with Jaro-Winkler ≥ `threshold` or a substring match, best first. |
| `GET /debaters/merge-info`, `/judges/merge-info`, `/institutions/merge-info` | Unpaginated admin lists (by name) with the context needed to decide a merge: debates/teams, prelims/elims/tournaments judged, team names. |
| `POST /debaters/merge` `{source_id, target_id}` | Moves the source's scores and team memberships onto the target, fills the target's empty fields from the source, deletes the source (and its profile). |
| `POST /judges/merge` `{source_id, target_id}` | Moves ballots and panel links, same field-filling. |
| `POST /institutions/merge` `{target_id, source_ids}` | Reassigns teams, debaters and judges; copies `code` if the target has none. |

Errors: `404` for a missing ID, `422` when source equals target (or `source_ids` is empty),
`409` when the records are provably different people — two debaters with scores on the same
ballot, or two judges on the same panel / with ballots for the same debate. Run
`POST /stats/refresh` afterwards to rebuild the materialized profiles.

`DELETE /judges/{id}` now returns `409` for a judge who has ballots or panel assignments, and
both person deletes remove a leftover profile row instead of failing on its foreign key.

## Score and history queries

All grouped per tournament (id, name, abbreviation, date). "Counted" means from a
non-discarded, non-forfeit ballot.

| Endpoint | Returns |
|----------|---------|
| `GET /debaters/{id}/scores?include_replies=false&normalized=false` | Every counted score per round (judge, side, position, score). `normalized=true` returns the tab's round scores instead (panel mean, replies dropped, iron max) with an `is_iron` flag. |
| `GET /stats/debater-scores?normalized=` | The same for every debater. |
| `GET /debaters/{id}/debates?stage=prelim\|elim` | Debates with side, opponent team and result. |
| `GET /debaters/{id}/teams` | Teams joined with their tournament. |
| `GET /judges/{id}/scores?include_replies=false` | Scores the judge gave, per round. |
| `GET /judges/{id}/debates?stage=` | Panel memberships (chair/trainee, whether a ballot was submitted). |
| `GET /stats/tournament-averages` | Mean and count of counted substantive prelim scores per tournament. |
