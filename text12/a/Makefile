.PHONY: up build down logs migrate demo test test-local fmt

COMPOSE ?= docker compose

up:
	$(COMPOSE) up --build -d
	@echo "API:  http://localhost:18099  (docs: /docs)"
	@echo "DB:   localhost:15499 (cloudgate/cloudgate)"

build:
	$(COMPOSE) build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f app

migrate:
	CLOUDGATE_DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@localhost:15499/cloudgate \
	alembic upgrade head

demo:
	./scripts/demo.sh

test:
	CLOUDGATE_DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@localhost:15499/cloudgate \
	python3 -m pytest -q

fmt:
	ruff format app tests || true
