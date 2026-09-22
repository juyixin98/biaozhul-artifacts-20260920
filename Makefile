.PHONY: all build run test unit-test vet fmt sqlc db db-stop migrate docker-build up down tidy

DB_URL ?= postgres://costlens:costlens@localhost:55432/costlens?sslmode=disable

all: vet build

build:
	go build ./...

run:
	DATABASE_URL='$(DB_URL)' COSTLENS_HTTP_ADDR=':8080' go run ./cmd/server

test:
	TEST_DATABASE_URL='$(DB_URL)' go test ./... -count=1

unit-test:
	go test ./internal/csvparse ./internal/decimalx ./internal/service -run 'TestParse|TestSqrt|TestEval' -count=1

vet:
	go vet ./...

fmt:
	gofmt -s -w .

sqlc:
	sqlc generate -f sqlc.yaml

tidy:
	go mod tidy

db:
	docker run -d --name costlens-test \
	  -e POSTGRES_USER=costlens -e POSTGRES_PASSWORD=costlens \
	  -e POSTGRES_DB=costlens -p 55432:5432 postgres:16-alpine
	@for i in $$(seq 1 30); do \
	  docker exec costlens-test pg_isready -U costlens >/dev/null 2>&1 && exit 0; \
	  sleep 1; \
	done; echo "db not ready"; exit 1

db-stop:
	-docker rm -f costlens-test

migrate:
	DATABASE_URL='$(DB_URL)' go run ./cmd/server -help >/dev/null 2>&1 || true
	@echo "migrations apply automatically at server start (COSTLENS_AUTOMIGRATE)"

docker-build:
	docker build -t costlens:latest .

up:
	docker compose up --build

down:
	docker compose down -v
