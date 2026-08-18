.PHONY: install dev eval test lint

install:
	python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

dev:
	docker compose up -d
	. .venv/bin/activate && incidentpilot

eval:
	. .venv/bin/activate && python -m eval.harness

test:
	. .venv/bin/activate && pytest -q

lint:
	. .venv/bin/activate && ruff check .
