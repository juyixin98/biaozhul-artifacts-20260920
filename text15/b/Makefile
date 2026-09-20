.PHONY: up down build test test-race demo logs fmt vet

up: ## Build and start MySQL + app (seeded demo data) on :18090
	docker compose up -d --build

down: ## Stop and remove containers (keeps volumes)
	docker compose down

build:
	go build ./...

fmt:
	gofmt -w . && go mod tidy

vet:
	go vet ./...

test: ## Unit + integration tests (needs proofcycle_test DB; see README)
	go test ./... -count=1

test-race:
	go test ./... -race -count=1

demo: ## Run the end-to-end demo against the running stack
	BASE_URL=http://localhost:18090 ./scripts/demo.sh

logs:
	docker compose logs -f app
