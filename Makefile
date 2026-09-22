DATABASE_URL ?= postgres://postgres:postgres@localhost:25432/domains?sslmode=disable

.PHONY: up down test test-unit build run

up:            ## start postgres + app (migrations run automatically)
	docker compose up -d --build

down:          ## stop and remove containers
	docker compose down

db:            ## start only postgres (for local go run / tests)
	docker compose up -d db

build:
	go build ./...

run: build     ## run the server locally against the compose database
	DATABASE_URL="$(DATABASE_URL)" \
	AUTH_CODE_KEY="000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f" \
	ENABLE_CLOCK_CONTROL=true \
	go run ./cmd/server

test-unit:     ## tests that need no database
	go test ./internal/domainname/ ./internal/clock/

test:          ## full suite against the compose database
	TEST_DATABASE_URL="$(DATABASE_URL)" go test -race ./...
