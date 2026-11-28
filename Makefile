.PHONY: dev prod test user import-registrations db-create db-indexes db-seed db-drop db-truncate db-reset

dev:
	.venv/bin/gunicorn app.main:app -k uvicorn_worker.UvicornWorker -w 2 --reload

# Production server: no --reload, access log to stdout, proxy headers trusted only from
# FORWARDED_ALLOW_IPS (the reverse proxy's address). --timeout covers POST /stats/refresh,
# which runs inside the request. See docs/deployment.md.
prod:
	.venv/bin/gunicorn app.main:app -k uvicorn_worker.UvicornWorker \
		-w $(or $(WORKERS),2) --bind 0.0.0.0:$(or $(PORT),8000) \
		--timeout 120 --graceful-timeout 30 --keep-alive 5 \
		--access-logfile - --forwarded-allow-ips "$(or $(FORWARDED_ALLOW_IPS),127.0.0.1)"

test:
	.venv/bin/pytest

# make user EMAIL=chair@example.com NAME="Chair" [ROLE=board|volunteer]; prompts for the password.
user:
	PYTHONPATH=. .venv/bin/python scripts/create_user.py --email "$(EMAIL)" --name "$(NAME)" --role "$(or $(ROLE),board)"

# make import-registrations CSV=reg.csv YEAR=2025 [MAP=map.json] [ARGS="--paid-on-submission --apply"]
# Dry run unless ARGS has --apply. See scripts/import_registrations.py.
import-registrations:
	PYTHONPATH=. .venv/bin/python scripts/import_registrations.py "$(CSV)" --year "$(YEAR)" $(if $(MAP),--map "$(MAP)") $(ARGS)

db-create:
	PYTHONPATH=. .venv/bin/python scripts/db.py create

# Adds the model indexes missing from an existing database (create-if-not-exists, non-destructive).
db-indexes:
	PYTHONPATH=. .venv/bin/python scripts/db.py indexes

db-seed:
	PYTHONPATH=. .venv/bin/python scripts/db.py seed

db-drop:
	@echo "This will DROP ALL TABLES (schema and data)."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted." && exit 1)
	PYTHONPATH=. .venv/bin/python scripts/db.py drop

db-truncate:
	@echo "This will TRUNCATE ALL TABLES (delete all rows, keep schema)."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted." && exit 1)
	PYTHONPATH=. .venv/bin/python scripts/db.py truncate

db-reset:
	@echo "This will DROP, recreate, and reseed all tables."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted." && exit 1)
	PYTHONPATH=. .venv/bin/python scripts/db.py reset
