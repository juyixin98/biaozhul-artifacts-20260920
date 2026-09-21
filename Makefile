.PHONY: help generate build test unit int up down demo migrate reconcile

help: ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

generate: ## regenerate sqlc Go code from db/queries
	sqlc generate

build: ## build server and reconcile binaries
	go build ./cmd/server ./cmd/reconcile

unit: ## fast unit tests (no database)
	go test ./internal/money/...

int: ## integration tests (auto-starts an ephemeral docker postgres)
	go test ./internal/integration/...

test: unit int ## all tests

up: ## start postgres + api via docker compose
	docker compose up --build -d

down: ## stop and remove the stack (keeps named volume unless -v is passed)
	docker compose down

demo: ## run the end-to-end simulated-transaction walkthrough
	./examples/demo.sh
