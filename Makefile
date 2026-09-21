.PHONY: up down build run sqlc migrate-copy test test-race fmt vet demo

DATABASE_URL ?= postgres://community:community@localhost:5432/communityvault?sslmode=disable
TEST_DATABASE_URL ?= $(DATABASE_URL)

up:
	docker compose up --build

down:
	docker compose down -v

build:
	go build ./...

run:
	DATABASE_URL='$(DATABASE_URL)' go run ./cmd/server

# Regenerate sqlc code after editing db/queries/*.sql
sqlc:
	sqlc generate
	$(MAKE) migrate-copy

# The migration runner embeds SQL from internal/migrate; keep it identical to migrations/.
migrate-copy:
	cp migrations/*.sql internal/migrate/

test:
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' go test ./...

test-race:
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' go test -race ./...

fmt:
	gofmt -w cmd internal

vet:
	go vet ./...

demo:
	scripts/demo.sh http://localhost:8080
