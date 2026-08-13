.PHONY: dev test db-create db-seed db-drop db-truncate db-reset

dev:
	.venv/bin/gunicorn app.main:app -k uvicorn_worker.UvicornWorker -w 2 --reload

test:
	.venv/bin/pytest

db-create:
	PYTHONPATH=. .venv/bin/python scripts/db.py create

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
