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
