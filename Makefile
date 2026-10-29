.PHONY: install test lint format typecheck check db-up db-down migrate dev frontend-install frontend-dev poc

PY ?= .venv/bin/python

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check backend
	$(PY) -m ruff format --check backend

format:
	$(PY) -m ruff format backend
	$(PY) -m ruff check --fix backend

typecheck:
	$(PY) -m mypy

check: lint typecheck test

migrate:
	$(PY) -m alembic upgrade head

dev:
	PYTHONPATH=backend $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

poc:
	PYTHONPATH=backend $(PY) -m scripts.poc.caption_diagnostics --video-id dev_tr_video_01

db-up:
	docker compose up -d db

db-down:
	docker compose down

