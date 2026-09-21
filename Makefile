.PHONY: help build test race cover lint run tidy docker-up docker-down docker-logs seed tidy-check

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "ForensicCore targets:\n"} \
	  /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build server and genseed into ./bin
	mkdir -p bin
	CGO_ENABLED=0 go build -trimpath -o bin/server ./cmd/server
	CGO_ENABLED=0 go build -trimpath -o bin/genseed ./cmd/genseed

test: ## Run unit/integration tests (sqlite, no CGO required)
	go test -count=1 ./...

race: ## Run tests with the race detector
	go test -race -count=1 ./...

cover: ## Write a coverage profile to coverage.out
	go test -coverprofile=coverage.out ./...
	go tool cover -func=coverage.out | tail -1

vet: ## Run go vet
	go vet ./...

tidy: ## Tidy modules
	go mod tidy

run: ## Run locally against sqlite (WHITELIST_DIRS must point at your images)
	DB_DRIVER=sqlite DB_DSN=./forensiccore-local.db \
	WHITELIST_DIRS=./samples \
	INVESTIGATOR_TOKEN=dev-inv ANALYST_TOKEN=dev-ana \
	go run ./cmd/server

seed: ## Generate sample images into ./samples
	mkdir -p samples
	go run ./cmd/genseed ./samples

docker-up: ## Build and start MySQL + API with docker compose
	docker compose up -d --build

docker-down: ## Stop the stack
	docker compose down

docker-logs: ## Tail API logs
	docker compose logs -f api
