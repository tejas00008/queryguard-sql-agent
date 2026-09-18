VENV ?= .venv
PY   := $(VENV)/bin/python

.PHONY: install db-sqlite db-postgres pg-up pg-down pg-verify bird eval eval-oracle ask test lint clean

install:
	uv venv --python 3.12 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

db-sqlite:
	$(PY) data/scripts/build_saas_db.py --target sqlite

pg-up:
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U postgres -d queryguard >/dev/null 2>&1; do sleep 1; done
	@echo "ready on localhost:5433"

db-postgres: pg-up
	$(PY) data/scripts/build_saas_db.py --target postgres

pg-verify:
	$(PY) data/scripts/verify_readonly.py

pg-down:
	docker compose down

bird:
	$(PY) data/scripts/fetch_bird.py

eval:
	$(PY) -m evaluation.harness --limit $(or $(N),15)

eval-oracle:
	$(PY) -m evaluation.harness --oracle --limit $(or $(N),50)

ask:
	@$(PY) -m queryguard ask "$(Q)" --show-sql

test:
	$(VENV)/bin/pytest -q

lint:
	$(VENV)/bin/ruff check src tests data evaluation
