.PHONY: help build fixtures reset ingest verify verify-fail test test-setup test-race fmt vet run acceptance clean-db

GO ?= go
DATABASE_URL ?= host=/var/run/postgresql user=admin dbname=forkindexer
HTTP_ADDR ?= 127.0.0.1:8080

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build: ## Build all binaries into bin/
	$(GO) build -o bin/forkindexerd ./cmd/server
	$(GO) build -o bin/indexer ./cmd/indexer
	$(GO) build -o bin/genfixtures ./cmd/genfixtures

fixtures: build ## (Re)generate testdata/ block streams with real SHA-256 hashes
	$(GO) run ./cmd/genfixtures --out testdata

run: build ## Start the HTTP API
	DATABASE_URL="$(DATABASE_URL)" HTTP_ADDR="$(HTTP_ADDR)" ./bin/forkindexerd

reset: build ## Wipe all indexed state (blocks, balances, cursor)
	DATABASE_URL="$(DATABASE_URL)" ./bin/indexer reset

ingest: build ## Ingest testdata/stream.ndjson once
	DATABASE_URL="$(DATABASE_URL)" ./bin/indexer ingest-file --file testdata/stream.ndjson

verify: build ## Reconstruct ledger from genesis and compare to incremental state
	DATABASE_URL="$(DATABASE_URL)" ./bin/indexer verify

test-setup: ## Create local test database (assumes peer auth as superuser-ish admin)
	psql "host=/var/run/postgresql user=admin dbname=postgres" -tAc "SELECT 1 FROM pg_database WHERE datname='forkindexer_test'" | grep -q 1 || \
		psql "host=/var/run/postgresql user=admin dbname=postgres" -c "CREATE DATABASE forkindexer_test"
	psql "host=/var/run/postgresql user=admin dbname=forkindexer" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" >/dev/null 2>&1 || true

test: ## Run unit + integration tests (needs test database; skips if absent)
	$(GO) test ./... -count=1

test-race: ## Run tests under the race detector
	$(GO) test -race ./... -count=1

fmt: ## Format all Go code
	$(GO) fmt ./...

vet: ## Static checks
	$(GO) vet ./...

acceptance: build ## End-to-end acceptance scenario against $(DATABASE_URL)
	DATABASE_URL="$(DATABASE_URL)" HTTP_ADDR="$(HTTP_ADDR)" ./scripts/acceptance.sh

clean-db: ## Drop and recreate both databases
	psql "host=/var/run/postgresql user=admin dbname=postgres" -c "DROP DATABASE IF EXISTS forkindexer" -c "CREATE DATABASE forkindexer"
	psql "host=/var/run/postgresql user=admin dbname=postgres" -c "DROP DATABASE IF EXISTS forkindexer_test" -c "CREATE DATABASE forkindexer_test"
