.PHONY: all sqlc build migrate seed run test test-race fmt vet docker up down clean

all: build

sqlc:
	sqlc generate

build:
	go build ./...

migrate:
	go run ./cmd/migrate

seed:
	go run ./cmd/seed

run:
	go run ./cmd/server

test:
	go test ./... -count=1

test-race:
	go test ./internal/integration -race -count=1

fmt:
	gofmt -w .

vet:
	go vet ./...

docker:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down -v

clean:
	docker compose down -v
