# AGENTS.md

FastAPI + SQLModel backend for tracking debate tournaments (Asian/Australs/WSDC two-team
format, prop vs. opp). Postgres-backed via `DATABASE_URL` in `.env`; falls back to
in-memory SQLite (`sqlite://`) if unset.

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

`client` is signed in as a **board** user; use `anon_client` / `volunteer_client` for
access tests. `tests/test_auth.py` enumerates every route from the OpenAPI schema (not
`app.routes`, which nests included routers lazily) and asserts each write and each
`/admin` route rejects anonymous (401) and volunteer (403) callers — so a new endpoint
is covered automatically, and a new public write must be added there deliberately.

## Layout

- `app/models/` — SQLModel table models. Each file typically defines the table model
  plus `*Create` / `*Public` / `*Update` variants (see `app/models/debate.py`). Relationships
  use string-quoted forward refs and `TYPE_CHECKING` imports to avoid circular imports.
- `app/api/v1/endpoints/` — one router module per resource; registered in
  `app/api/v1/router.py`. Endpoints use `SessionDep` and `get_or_404` from `app/api/deps.py`.
  Uniqueness conflicts are caught as `IntegrityError` and turned into `409`.
- `app/services/stats/` — pure computation over ballots/debates for tournament, debater,
  and judge statistics. `history.py` holds the raw score / debate / team history queries
  (per-tournament groupings) built on `core`.
- `app/services/text.py` — pure name/text normalization (`normalize_text`, `name_key`,
  `split_name`, `tidy_case`, `institution_key`, `jaro_winkler`). No DB access.
- `app/services/institutions.py` — `find_matching_institution` (key equality, used by the
  importer) and `similar_institutions` (fuzzy, human-facing search only — never auto-match
  on Jaro-Winkler).
- `app/services/merge.py` — debater/judge/institution merges (one commit each; raises
  `MergeNotFound`/`MergeInvalid`/`MergeConflict`, mapped to 404/422/409 by
  `merge_errors()` in `app/api/deps.py`) plus the duplicate finders and merge-info lists.
- `app/services/tabbycat.py` — importer that pulls a tournament from a
  [Tabbycat](https://tabbycat.readthedocs.io) instance. Tabbycat's model (BP-style 4-team
  debates, multiple motions per round, arbitrary scoring criteria) is richer than this
  app's two-team schema, so anything that doesn't map is skipped, not aborted — see the
  `skipped` list in `ImportReport`. There is no `tabbycat_id` column, so re-importing the
  same slug is not idempotent and raises `409`.
- Static routes such as `/merge`, `/duplicates`, `/similar`, `/merge-info` must be declared
  **before** `/{id}` in their router, or the int path parameter claims them and returns 422.
- Auth: `app/models/user.py` (`User`, `AuthSession`), `app/core/security.py` (argon2 via
  `pwdlib`, token hashing), `app/services/auth.py` (create/resolve/revoke sessions), and
  `app/api/v1/endpoints/auth.py`. Dependencies in `app/api/deps.py`: `CurrentUserDep`,
  `require_board` / `BoardUserDep`, and `require_board_for_writes`. Access is set where
  routers are included in `app/api/v1/router.py`, not per endpoint: the public resource
  routers get `require_board_for_writes` (reads open, writes board-only), and the
  `app/api/v1/endpoints/admin/` package (mounted at `/admin`) gets `require_board`. New
  management endpoints go in that package so they inherit it.
- Management console (all under `app/api/v1/endpoints/admin/`, board-only):
  - `app/api/crud.py` — `add_crud_routes()` generates POST / GET list / GET / PATCH / DELETE
    for a plain admin resource from its models, a `ListParams` filter subclass (each field is
    an equality filter), `parents` (FK fields 404-checked before insert/update) and an
    optional `on_save` hook. Use it for new plain-CRUD tables; hand-write anything that
    computes or touches several tables (see `convert_lead` in `admin/outreach.py`). PATCH
    rejects explicit nulls on NOT NULL columns with 422 instead of letting them 409.
  - Member schools: `Membership` (one per school per calendar year), `SchoolContact`,
    `SchoolAchievement`, `SchoolParticipation` (manual history for tournaments that were
    never imported), `Feedback` (optional institution / tournament / judge subject) and
    `OutreachLead`. `app/services/schools.py` derives membership standing
    (member / lapsed / former / never + `irregular`) and builds the schools list and a
    school's detail; tournament history merges imported Team → Tournament rows with the
    manual ones.
  - `Institution` has `kind` and `district` but deliberately **no notes column**:
    `InstitutionPublic` is served by the public `GET /institutions`, so anything internal
    goes on the admin-only tables.
- `app/services/registrations.py` + `scripts/import_registrations.py` (`make
  import-registrations`) — the yearly Google Form registration import. Headers are matched
  by prefix so small form edits don't break it. Resolution order per row: explicit id in the
  mapping → exact `institution_key` match → `"new"` in the mapping → new institution if
  nothing is even similar → otherwise *ambiguous* (blocks the import). The exact match has
  to come before `"new"`, or a re-run would try to recreate the schools the first run made.
  Dry runs do the whole import and roll back; re-runs are no-ops (fill-blanks only). Tests
  use a fake CSV with the real form headers — never commit the real sheet (student PII).
- Contacts have a coarse `role` (incl. `student`) plus a free-text `title`, and secondary
  email/phone. Membership `payment_proof_url` / `request_letter_url` are validated to
  http(s) (`http_url` in `app/models/membership.py`) because the console renders them as
  links.
- **New FKs to institution / judge / tournament need three things**: repoint them in the
  matching merge in `app/services/merge.py`, count them in the `delete_*` 409 guard (or make
  them `ondelete="SET NULL"` when the row should outlive the parent — feedback and
  achievements do this for tournaments and judges, outreach leads for institutions), and a
  test. `merge_institutions` refuses (409) when two of the merged schools hold a membership
  for the same year rather than dropping one.
- Schema changes: there are no migrations. `create_all` adds new tables on startup but never
  new columns, so a new column on an existing table needs a dev DB reset or a one-off
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Enum columns are native Postgres enums whose
  labels are the member **names** (`SCHOOL`, not `school`), so an ALTER must create the type
  with those labels first.
- `app/core/config.py` — `pydantic-settings` config read from `.env` with `extra="ignore"`,
  so a typo'd env var name is silently ignored rather than erroring.

## Conventions

- Python 3.14, full type hints, `X | None` union syntax (not `Optional`).
- Keep model/schema/endpoint changes consistent: adding a field to a table model usually
  means updating its `*Create`/`*Public`/`*Update` counterparts too.
- Match existing router error-handling patterns (`get_or_404`, catching `IntegrityError`
  for conflicts) rather than introducing new patterns.
- No linter/formatter is configured in this repo — match surrounding style by hand.

## Stats semantics

Deliberate choices, mostly inherited from the Java app (v1):

- **Debater speaker average is prelim-only.** `average_speaker_score`, `speaker_score_stdev`,
  `total_speeches`, `speaker_rank` and `speaker_score_percentile` are computed from prelim
  round scores only. `iron_person_count` still counts prelims and elims.
- **Judge `score_stdev` uses substantive speeches only**, since replies are on a half scale.
  `average_reply` is still reported separately.
- Round scores are normalized as panel mean → drop replies → max over positions spoken
  (iron merge).
- Kept from v2 on purpose: leniency/harshness as means, competition ranking for ties, and
  quartiles in the global distribution.
- Tournament stats are computed per request; debater/judge profiles are materialized by
  `POST /stats/refresh` and go stale after merges or imports until it is re-run.
