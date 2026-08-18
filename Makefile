.PHONY: install dev eval test lint fmt stack stack-down

install:
	uv venv --python 3.12 .venv
	. .venv/bin/activate && uv pip install -e ".[dev]"

dev:  ## run the API (durable orchestration wired in Phase 3)
	. .venv/bin/activate && incidentpilot

stack:  ## bring up the demo target system (services + prometheus + redis)
	docker compose up -d

stack-down:
	docker compose down -v

eval:
	. .venv/bin/activate && python -m eval.harness

test:
	. .venv/bin/activate && pytest

lint:
	. .venv/bin/activate && ruff check .

fmt:
	. .venv/bin/activate && ruff check --fix . && ruff format .
