.PHONY: help dev db-up db-down migrate sqlc test test-unit test-integration build demo worker fmt sync-check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

dev: ## Run the API with an in-process worker (uses local compose Postgres)
	DATABASE_URL='postgres://clearsettle:clearsettle@127.0.0.1:35432/clearsettle?sslmode=disable' \
	go run ./cmd/api

db-up: ## Start PostgreSQL in Docker (host port 35432)
	docker compose up -d postgres

db-down: ## Stop and remove the database container
	docker compose down

db-reset: db-down db-up ## Drop all data and restart Postgres
	sleep 4

sqlc: ## Regenerate the sqlc store
	docker run --rm -v "$$PWD:/src" -w /src sqlc/sqlc:1.27.0 generate

build: ## Build api and worker binaries
	go build -o bin/api ./cmd/api
	go build -o bin/worker ./cmd/worker

test: ## Run every test (needs Postgres)
	go test -race -count=1 ./...

test-unit: ## Run unit tests that need no database
	go test -short ./internal/...

test-integration: ## Run PostgreSQL-backed end-to-end tests
	go test -race -count=1 ./tests/integration/

worker: ## Run the standalone background worker
	DATABASE_URL='postgres://clearsettle:clearsettle@127.0.0.1:35432/clearsettle?sslmode=disable' \
	go run ./cmd/worker

demo: ## Run the end-to-end curl demo against localhost:8080
	./scripts/demo.sh

fmt: ## Format all Go code
	gofmt -w cmd internal tests

sync-check: ## Verify embedded migrations match the canonical SQL
	./scripts/check-migrations-sync.sh
