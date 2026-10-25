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

`conftest.py` sets `settings.init_db_on_startup = False`: otherwise `TestClient(app)` runs
the app lifespan, whose `create_all` and default-category seed would write to the `.env`
database.

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
    an equality filter, except those named in its `custom` class var, which go to the
    `filter_hook(statement, params)`), `parents` (FK fields 404-checked before
    insert/update) and an optional `on_save(session, obj, user)` hook (validation, stamping
    who changed it). Use it for new plain-CRUD tables; hand-write anything that
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
  - Finance (`admin/finance.py`, `app/services/finance.py`): `Account` (opening balance +
    `opening_date`), `FinanceCategory` (income / expense; defaults seeded by
    `ensure_default_categories()` at startup and in `make db-create`), `Subscription` and
    `Transaction`. The cross-field ledger rules are `validate_transaction()` (run from the
    factory's `on_save`, so creates and PATCHes both go through it); `account_balances()`
    counts money by when it moved (`coalesce(reimbursed_on, occurred_on)`, pending
    reimbursements excluded) while `finance_summary()` counts spending by `occurred_on`.
    Actions that create transactions for something else — `record_subscription_payment`,
    `record_membership_payment`, `mark_reimbursed` — are hand-written endpoints. The service
    raises `FinanceInvalid` / `FinanceConflict`; `finance_errors()` maps them to 422 / 409.
    The CSV export escapes formula-leading cells (`csv_cell`).
  - Squads (`admin/squads.py`, `app/services/squads.py`): `Squad` (kind national /
    development + a free-text `season` such as `2025/26`, unique per kind), `SquadMember`
    (a `Debater`, role, dates; unique per squad) and `SquadStaff` (a name + free-text role,
    optional `person_id`). `GET /admin/squads/{id}/roster` returns the squad with debater and
    school names in one call. Squad lists order by season only: enum columns sort by name on
    SQLite but by declaration order on Postgres, so never rely on ordering by an enum.
  - Trips (`admin/trips.py`, `app/services/trips.py`): `Trip` (free-text `event` such as
    `WSDC` groups trips for forecasts), `TripTraveller` (just a name: not linked to debaters,
    judges or people), `TripBudgetLine` and `TripDirectCost`. **A trip's actual cost has two
    sources.** The DC pays from its accounts: ledger expenses carrying `Transaction.trip_id` +
    `trip_cost_category` (+ optional `trip_traveller_id`; a pending reimbursement counts,
    it's spent). Families pay some costs directly: `TripDirectCost`, which is deliberately
    **never a ledger transaction** because the money never touched a DC account, so the
    finance dashboard and balances only see the DC's own spending. `trip_costs()` adds both
    per category and per traveller (plus a "Shared" row for lines naming nobody) and divides
    by the traveller count for per-person figures. `trip_forecast()` averages per-person cost
    by category across completed same-event trips (case-insensitive) that had travellers and
    some actual cost, then scales to a traveller count; `apply_forecast` / `copy_budget`
    (optionally scaled by travellers ÷ source travellers) write budget lines. The rules live
    in the service (`TripInvalid` / `TripConflict`, mapped to 422 / 409 by `trip_errors()`).
    Trip fields on a transaction are validated in `validate_transaction()`: expenses only, a
    category whenever a trip is set, and the traveller must be on that trip. Budget lines and
    direct costs check the same traveller rule in their `on_save`.
  - `add_crud_routes` also takes an optional `before_delete(session, obj)` hook, used by
    trips, workshops and people to refuse a delete with a clear message ("unlink or delete
    its N ledger expenses first") before the FK would produce a generic 409.
  - `Person` (`admin/people.py`, `app/services/people.py`) is the DC's own people;
    `User.person_id` links a console account to one (`ON DELETE SET NULL`). Board roles,
    transactions and subscriptions are history, so a person named on any of them can't be
    deleted: `person_references()` + a `before_delete` hook give a 409 that says which.
    Facilitator links cascade and tasks become unassigned. `person_detail()` gathers
    everything about one person; `board()` splits `BoardRole` terms into current / upcoming
    / past.
  - Tasks (`admin/tasks.py`, `app/services/tasks.py`): only the board writes them. The one
    volunteer-facing endpoint is `GET /api/v1/me/tasks` (`app/api/v1/endpoints/me.py`),
    mounted **outside** `/admin` with `CurrentUserDep` and deliberately read-only: it
    returns the tasks of the signed-in user's linked person. Being a GET outside `/admin`, it
    isn't in `test_auth.py`'s `_protected_routes()`; `tests/test_tasks.py` covers it
    explicitly (anon 401, own tasks only, no write methods). Don't add volunteer writes
    without revisiting that test.
  - Workshops (`admin/workshops.py`, `app/services/workshops.py`): `Workshop`,
    `WorkshopSchool` (institution + student count; unique per workshop) and
    `WorkshopFacilitator` (person + role). Money is ledger rows with
    `Transaction.workshop_id` (income or expense, never a transfer — `validate_transaction`);
    a workshop with ledger rows can't be deleted (`before_delete`). `Feedback.workshop_id`
    and the task links are `SET NULL`. `merge_institutions` combines two merged schools'
    attendance at one workshop into one row with the students added up.
  - Tournament ops (`admin/tournament_ops.py`, `app/services/tournament_ops.py`) and the
    judge registry (`admin/judges.py`, `app/services/judges.py`). **Admin data about
    tournaments and judges goes in side tables** — `TournamentOps` (1:1 settings; no row =
    defaults), `TournamentRegistration`, `JudgeRecord` (1:1), `JudgeAffiliation` — because
    `TournamentPublic` / `JudgePublic` are served by the open API. Ops rows combine the
    board's registrations with counts derived from the tab in grouped queries
    (`_teams_by_school`, `_judges_by_school`; trainee seats don't count as judges
    provided); overrides win, and a tournament with no imported teams reports fielded /
    provided as `None` rather than 0. Judge involvement is computed live from `DebateJudge`
    (so it's current right after an import), unlike the materialized `JudgeProfile`. Merges:
    `merge_institutions` repoints registrations (409 when two merged schools registered for
    the same tournament) and affiliations; `merge_judges` moves affiliations (dropping exact
    duplicates) and the `JudgeRecord` (filling the target's blanks if it has one). Judge and
    tournament deletes cascade to these tables; institution deletes are guarded.
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
  for the same year rather than dropping one. `Transaction.institution_id` /
  `tournament_id` / `membership_id` are `SET NULL` (the money still happened) and the
  institution merge repoints them.
- **Debater FKs**: `SquadMember.debater_id` is a plain FK. `merge_debaters` repoints it and,
  when both debaters are on the same squad, drops the source's row (like `TeamMember`);
  `delete_debater` is a 409 while the debater is on a squad. Trip links are lenient where the
  history should survive: `Trip.squad_id` / `tournament_id` and `TripTraveller` links on
  budget lines, direct costs and transactions are `SET NULL`; trip children cascade with the
  trip; `Transaction.trip_id` is a plain FK, so deleting a trip that has ledger expenses is a
  409. Adding the trip columns to an existing dev database is a one-off additive `ALTER`
  (see Schema changes). Public `GET /debaters` takes `q` (case-insensitive name contains,
  LIKE wildcards escaped) so the console's roster picker can search server-side.
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
