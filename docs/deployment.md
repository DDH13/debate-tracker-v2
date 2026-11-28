# Deploying to production

The API is a long-running gunicorn server (FastAPI + SQLModel) in front of Postgres. The
UI (`debate-tracker-v2-ui`) is a static Vite build, deployed on Vercel.

## Host requirements

You need a **persistent process with a local disk**, such as a VM, Render, Fly.io, Railway
or a container platform with a volume. It can't run as serverless functions (for example
Vercel's Python runtime), because three features assume a process that stays up:

- **Console imports.** A background thread runs the Tabbycat checks and imports. It
  heartbeats every 30 s and stages each checked tournament's upstream responses on disk
  (`IMPORT_STAGING_DIR`) until the import runs. If a function freezes between requests,
  that thread stops mid-job, and the staged files vanish with the instance.
- **The public page cache and the login throttle.** Both are in-process. A fresh instance
  per request makes them useless.
- **`POST /stats/refresh`.** It runs inside the request and can take a while on a large
  database, so it needs the 120 s gunicorn timeout.

Put `IMPORT_STAGING_DIR` on a persistent volume. It holds speaker emails, so the volume
shouldn't be readable by anything else.

## Environment

Set these in production. Everything else can keep its default (see the table in the
README).

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `production`. Refuses to start with SQLite, `CORS_ORIGINS=["*"]` or `SEED_ON_STARTUP=true`. Forces a `Secure` session cookie and turns `/docs` and `/openapi.json` off. |
| `DATABASE_URL` | The Postgres URL. A `postgres://` or `postgresql://` URL is rewritten to use psycopg 3 automatically. |
| `CORS_ORIGINS` | The UI's origin(s) as a JSON list, e.g. `["https://stats.example.org"]`. List these in both topologies below: the cross-site write guard only accepts writes from listed origins, the API's own host, or the forwarding proxy's `X-Forwarded-Host`. |
| `SESSION_COOKIE_SAMESITE` | `lax` for topology A, `none` for topology B. |
| `FORWARDED_ALLOW_IPS` | The reverse proxy's address(es), passed to gunicorn's `--forwarded-allow-ips`. The default is `127.0.0.1`. Without it, every client appears to come from the proxy's IP, so the login throttle would lock out everyone at once. Use `*` only when the proxy is the only way to reach the API. |
| `IMPORT_STAGING_DIR` | A path on the persistent volume. The Docker image uses `/data/import-staging`. |
| `INIT_DB_ON_STARTUP` | Leave it `true`, which runs `create_all` and seeds the default finance categories. Workers take a Postgres advisory lock for this, so they don't race on a fresh database. |
| `LOG_LEVEL` | `INFO`, the default. `DEBUG` logs speaker names from imports. |
| `WORKERS`, `PORT` | gunicorn worker count and port. Defaults are `2` and `8000`. |

## Running

```bash
# On a host with the repo checked out:
.venv/bin/pip install -r requirements.txt
make prod                       # gunicorn without --reload, access log to stdout

# Or with the image:
docker build -t debate-tracker-api .
docker run --env-file .env.production -p 8000:8000 -v dt-data:/data debate-tracker-api
```

Release steps for a new database:

1. `make db-create` (or just start the server, which does the same).
2. Run `make user EMAIL=... NAME="..."` for the first board account.
3. Run `make db-indexes` after upgrading an existing database. `create_all` never adds
   indexes or columns to tables that already exist, and there are no migrations. A new
   column needs a one-off `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; see AGENTS.md.

`GET /health` runs `SELECT 1` and returns 503 when Postgres is unreachable. Point the
platform's health check at it.

## Topology A (recommended): one origin through a rewrite

The UI's `vercel.json` rewrites `/api/*` to the API host. The browser only ever talks to
the UI's domain, so the session cookie is first-party:

- `SESSION_COOKIE_SAMESITE=lax`, which is the default.
- `CORS_ORIGINS=["https://<ui-domain>"]`. A rewritten request still carries the UI's
  `Origin`.
- To change the API host, edit the `destination` of the `/api/:path*` rewrite in the UI's
  `vercel.json`. Rewrites can't read environment variables.

## Topology B: split origins

The UI calls the API's own domain directly:

- Build the UI with `VITE_API_BASE_URL=https://<api-domain>/api/v1`. The client then sends
  credentials cross-origin.
- `SESSION_COOKIE_SAMESITE=none`. The cookie is `Secure` automatically in production.
- `CORS_ORIGINS=["https://<ui-domain>"]`. Credentialed CORS is turned on for that list.
- Browsers that block third-party cookies (Safari, and Chrome with that setting) won't keep
  the console session. That's the main reason to prefer topology A.

## Checklist

- [ ] `ENVIRONMENT=production`, `DATABASE_URL` and `CORS_ORIGINS` are set, and the server starts.
- [ ] `curl https://<api>/health` returns `{"status":"ok","database":"ok"}`.
- [ ] `curl https://<api>/docs` returns 404.
- [ ] Console sign-in works from the UI, and the cookie is `Secure; HttpOnly`.
- [ ] `IMPORT_STAGING_DIR` is on a volume that survives a restart.
- [ ] Postgres backups are scheduled (the app doesn't do this).
