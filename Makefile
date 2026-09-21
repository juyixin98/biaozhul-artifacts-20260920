.PHONY: up down build test test-unit test-integration lint run fmt

DATABASE_URL ?= postgres://desklens:desklens@localhost:5432/desklens?sslmode=disable

up:
	docker compose up -d --build

down:
	docker compose down

build:
	go build ./...

fmt:
	gofmt -l -w .

lint:
	go vet ./...

run:
	DATABASE_URL=$(DATABASE_URL) go run ./cmd/desklens

# Unit tests need no database.
test-unit:
	go test ./internal/classify/ ./internal/policy/ ./internal/timeutil/

# Integration tests need Postgres (docker compose up -d postgres).
# Each test provisions its own throwaway database on the same server.
test-integration:
	TEST_DATABASE_URL=$(DATABASE_URL) go test -race -count=1 ./internal/integrationtest/

test: lint test-unit test-integration
