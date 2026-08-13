.PHONY: dev test

dev:
	.venv/bin/gunicorn app.main:app -k uvicorn_worker.UvicornWorker -w 2 --reload

test:
	.venv/bin/pytest
