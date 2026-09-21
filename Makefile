.PHONY: build run test sqlc migrate docker-up docker-test docker-down

build:
	go build ./...

run:
	go run ./cmd/server

# Requires DATABASE_URL, e.g.:
#   DATABASE_URL=postgres://sircc:postgres@localhost:5432/sircc?sslmode=disable make test
test:
	go test ./...

sqlc:
	sqlc generate

docker-up:
	docker compose up --build

docker-test:
	docker compose --profile test run --rm test

docker-down:
	docker compose down -v
