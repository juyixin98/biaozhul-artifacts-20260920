.PHONY: up down build run test test-race generate fmt logs clean-db

up:
	docker compose up -d --build

down:
	docker compose down

build:
	go build ./...

run:
	go run ./cmd/server

test:
	go test ./... -count=1

test-race:
	go test ./internal/integration/ -race -count=3

generate:
	sqlc generate

fmt:
	gofmt -s -w .

logs:
	docker compose logs -f app

# Wipe the database volume (full reset including migrations).
clean-db:
	docker compose down -v
