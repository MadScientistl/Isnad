# Isnad developer entry points. `make help` lists them.
SHELL := /bin/bash
.PHONY: help setup dev dev-import test test-fast evals ontogen demo gate clean

help:
	@echo "make setup      one-command setup: database, Python deps, compile, web deps, seed"
	@echo "make dev        API (127.0.0.1:8000) + UI (localhost:5173); logs in var/log/"
	@echo "make dev-import same, with ontology import enabled (ISNAD_ALLOW_IMPORT=1)"
	@echo "make test       full test suite except live API tests (about 10 min, needs the database)"
	@echo "make test-fast  quick subset: skips perf-marked and live tests, stops at first failure"
	@echo "make evals      agent eval suite with the mock model, 3 seeds, markdown report in evals/results/"
	@echo "make ontogen    ontology-generator eval: baseline and mock cassettes (no API key needed)"
	@echo "make demo       scripted end-to-end demo (headless, deterministic, ~30 s)"
	@echo "make gate       Phase 1 feasibility benchmark (slow; seed 1, scale 1.0)"
	@echo "make clean      remove build output, caches, logs, stored uploads and generated workspaces"

setup:
	./scripts/setup.sh

dev:
	./scripts/dev.sh

dev-import:
	./scripts/dev.sh --import

test:
	uv run pytest -m "not live"

test-fast:
	uv run pytest -m "not live and not perf" -x -q

evals:
	uv run python -m evals --mode mock --seeds 1,2,3 --md

ontogen:
	uv run python -m evals.ontogen --mode baseline --md
	uv run python -m evals.ontogen --mode mock --md

demo:
	uv run python scripts/demo_phase6.py --model mock

gate:
	uv run python -m bench.gate --seed 1 --scale 1.0

# Does not touch the database or docker volumes (use `docker compose down -v` for that).
clean:
	rm -rf build .pytest_cache var/log var/ingest web/dist
	find . -name __pycache__ -type d -not -path "./web/node_modules/*" -not -path "./.venv/*" -prune -exec rm -rf {} +
	@echo "kept: .venv, web/node_modules, workspaces/ (delete imported ones by hand), the database"
