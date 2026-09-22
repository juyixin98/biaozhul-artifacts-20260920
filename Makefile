.PHONY: build test test-unit test-int gen sqlc up down tidy

build:
	go build ./...

gen:
	go run ./cmd/genpng samples

sqlc:
	sqlc generate

test-unit:
	go test ./internal/domain/... ./internal/compose/... -count=1

test-int:
	RENDERQ_TEST_DATABASE_URL=$${RENDERQ_TEST_DATABASE_URL:-postgres://renderq:renderq@localhost:5432/postgres?sslmode=disable} \
	  go test ./tests/... -count=1 -v

test: test-unit test-int

tidy:
	go mod tidy

up:
	docker compose up --build db api

down:
	docker compose down -v
