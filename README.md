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

## Member schools and outreach

Board-only endpoints under `/api/v1/admin`, behind the [console sign-in](#authentication):

| Endpoint | Purpose |
|---|---|
| `GET /admin/schools?year=` | Every institution with its membership standing for `year` (default: this year): `member`, `lapsed` (member last year, not this), `former`, `never`, plus `irregular` when they've missed a year since first joining. Also the year's membership, tournament count / latest tournament, primary contact and open feedback count. |
| `GET /admin/schools/{id}?year=` | One school: the summary above plus memberships, contacts, achievements, feedback, outreach leads and tournament history (imported tournaments merged with manual entries). |
| `/admin/memberships`, `/admin/school-contacts`, `/admin/school-achievements`, `/admin/school-participations`, `/admin/feedback`, `/admin/outreach` | CRUD (`POST`, `GET` with filters, `GET/PATCH/DELETE /{id}`). One membership per school per year (409 on a duplicate); one primary contact per school. |
| `POST /admin/outreach/{id}/convert` | Turns a lead into a member school in one step: links or finds (by name key) or creates the institution, adds the year's membership (reusing an existing one), and adds the lead's contact person. |

Fees are LKR decimals and come back as strings (`"7500.00"`). Institutions also have public
`kind` (school / university / other) and `district` fields.

## Finance and DC people

Board-only, under `/api/v1/admin`. Amounts are LKR decimals returned as strings; a
transaction can also carry the `original_amount` + `original_currency` actually paid (a USD
Zoom bill).

| Endpoint | Purpose |
|---|---|
| `/admin/people` | CRUD for the DC's own people (board, volunteers, staff). Squad debaters are `Debater`s and school contacts live per school, so neither is here. A console user links to a person via `person_id` on `/admin/users`. |
| `/admin/accounts`, `/admin/finance-categories` | CRUD. An account has an opening balance as of an `opening_date`. Categories are income or expense; the defaults are seeded at startup and by `make db-create`. Deleting either while transactions or subscriptions use it is a 409 — deactivate instead. |
| `/admin/transactions` | The ledger: CRUD with filters (`kind`, `category_id`, `account_id` on either side of a transfer, `reimbursement_status`, `paid_by_person_id`, `membership_id`, `subscription_id`, `institution_id`, `tournament_id`, `trip_id`, `date_from`, `date_to`, `q`). Newest first. An expense can be tagged to a trip (`trip_id` + `trip_cost_category`, optional `trip_traveller_id`). |
| `GET /admin/transactions/export.csv` | The same filters as a CSV download (no paging). Text cells starting with `= + - @` are prefixed with `'` so spreadsheets don't run them as formulas. |
| `POST /admin/transactions/{id}/mark-reimbursed` | Pays back a pending reimbursement: date and the account it was paid from. |
| `/admin/subscriptions`, `POST /admin/subscriptions/{id}/record-payment` | Recurring bills. Recording a payment adds the expense (prefilled from the subscription) and moves `next_renewal_on` on one cycle (31 Jan + 1 month = 28/29 Feb). |
| `POST /admin/memberships/{id}/record-payment` | Adds a membership fee as income (category "Membership fees", linked to the membership and school) and fills the membership's blank `paid_on` / `fee_lkr`. Once per membership. |
| `GET /admin/finance/summary?start=&end=` | Income, expenses and net for the period (default: this calendar year), by month and by category, plus what's owed to people, the monthly recurring cost of active subscriptions, renewals overdue or due in 30 days, and account balances. |
| `GET /admin/finance/balances?as_of=` | Account balances. |

Ledger rules (422 when broken): income and expenses need a category of the same kind;
a transfer needs two different accounts and no category; every transaction needs an account
except a **pending reimbursement** — money someone spent out of their own pocket, which
hasn't left a DC account yet. Only expenses can be reimbursed, and a reimbursement names the
person who paid.

Balances are the opening balance plus income, minus expenses, minus transfers out, plus
transfers in, counting only money that moved on or after the account's opening date (older
transactions are taken as already in the opening balance). A pending reimbursement isn't
counted until it's paid back, and then on the date it was paid back. Reports count spending
by the date it happened, so a pending reimbursement is already an expense there.

## Squads and trips

Board-only, under `/api/v1/admin`. The National Team and Development Squad for each season,
and the trips abroad (WSDC and others) with their budget, actual costs and a forecast.

| Endpoint | Purpose |
|---|---|
| `/admin/squads` | CRUD (filters `kind`, `season`). `season` is free text such as `2025/26`; one squad per kind and season (409). |
| `GET /admin/squads/{id}/roster` | The squad with its members (debater name, school, role, dates) and staff in one call. |
| `/admin/squad-members` | CRUD (filters `squad_id`, `debater_id`). A debater is on a squad once (409). |
| `/admin/squad-staff` | CRUD (filter `squad_id`): a name, a free-text role (coach, manager) and an optional `person_id`. |
| `/admin/trips` | CRUD (filters `status`, `event`, `squad_id`). `event` is free text (`WSDC`) and groups trips for forecasts. Deleting a trip removes its travellers, budget and direct costs, but is a 409 while ledger expenses are tagged to it. |
| `/admin/trip-travellers`, `/admin/trip-budget-lines`, `/admin/trip-direct-costs` | CRUD (filter `trip_id`). Travellers are just names. A budget line or cost may name one traveller of its own trip (422 otherwise). |
| `GET /admin/trips/{id}/costs` | Budget, paid by the DC, paid directly, actual and variance by category and by traveller (plus a "Shared" row), with totals and per-person figures. |
| `GET /admin/trips/forecast?event=&travellers=&exclude_trip_id=` | Per-person cost of past completed trips with the same event, averaged by category, and that average scaled to `travellers`. |
| `POST /admin/trips/{id}/copy-budget` `{from_trip_id, scale, replace}` | Copies another trip's budget lines; `scale` multiplies by this trip's travellers ÷ the source's. |
| `POST /admin/trips/{id}/apply-forecast` `{replace}` | Adds a budget line per forecast category, sized for this trip's travellers (422 with no history or no travellers). |

Trip money comes from two sources only. **The DC pays** from its accounts: ordinary ledger
expenses tagged with the trip, so they appear in the finance dashboard, balances and CSV
(which gains `trip` and `trip_category` columns). **Families pay some costs directly**: these
are recorded on the trip as direct costs and are never in the ledger, since the money never
touched a DC account. A trip's actual cost is the two added together; a pending
reimbursement counts as spent. Per-person figures divide by the number of travellers.

## Importing a year's school registrations

Schools register each year through the DC's Google Form. Export the responses tab as CSV
(File → Download → CSV) and import it — a **dry run by default**:

```bash
make import-registrations CSV=reg-2025.csv YEAR=2025
```

Each row resolves to an institution by exact name key (`St. Joseph's` = `St Josephs`). Rows
with only *similar* matches are listed as AMBIGUOUS with suggestions and block the import —
fuzzy matches are never applied automatically. Decide them in a JSON mapping file
(`<institution id>` to link, `"new"` to create) and apply:

```json
{"WESLEY COLLEGE COLOMBO": 108, "Gateway College, Kandy": "new"}
```

```bash
make import-registrations CSV=reg-2025.csv YEAR=2025 MAP=map-2025.json ARGS="--paid-on-submission --apply"
```

Per school this creates the year's membership (registration date, payment proof and request
letter links; several submissions for one school collapse into one membership), adds each
representative as a contact (WhatsApp numbers normalized to `+94…`, a teacher in charge
preferred as primary), and closes the school's open outreach leads. `--fee 5000` records a
fee; `--paid-on-submission` marks memberships paid on the submission date when a payment
proof is attached. Re-running the same sheet changes nothing, and existing values are only
ever filled in, never overwritten.

Keep the CSV and mapping file out of the repo — they hold students' contact details, and the
mapping's ids are specific to one database.

## Database management

```bash
make db-create    # create tables (SQLModel.metadata.create_all) + default finance categories
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
| `GET /debaters?q=` | The public debater list takes `q`: a case-insensitive "name contains" search (also `institution_id`, `offset`, `limit`), used by the console's squad picker. |
| `GET /debaters/duplicates?by=name\|name_birthdate` | Groups of debaters sharing a (case-insensitive) name, optionally also a birthdate. |
| `GET /judges/duplicates` | Same, for judges. |
| `GET /institutions/similar?name=&threshold=0.8` | Institutions with Jaro-Winkler ≥ `threshold` or a substring match, best first. |
| `GET /debaters/merge-info`, `/judges/merge-info`, `/institutions/merge-info` | Unpaginated admin lists (by name) with the context needed to decide a merge: debates/teams, prelims/elims/tournaments judged, team names. |
| `POST /debaters/merge` `{source_id, target_id}` | Moves the source's scores, team memberships and squad memberships onto the target, fills the target's empty fields from the source, deletes the source (and its profile). |
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
