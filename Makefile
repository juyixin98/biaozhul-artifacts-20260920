.PHONY: up down build run test test-race demo fmt vet migrate-check

# Pick non-standard host ports by default to avoid clashes on busy dev boxes.
MYSQL_PORT ?= 23306
API_PORT   ?= 39071

build:
	go build -o /tmp/proofcycle-server ./cmd/server

run:
	STORAGE_ROOT=./data/files HTTP_ADDR=:${API_PORT} \
	MYSQL_DSN="proof:proof@tcp(127.0.0.1:${MYSQL_PORT})/proofcycle?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true" \
	go run ./cmd/server

up:
	MYSQL_PORT=${MYSQL_PORT} API_PORT=${API_PORT} docker compose up --build -d

down:
	docker compose down

test:
	PROOFCYCLE_TEST_DSN="proof:proof@tcp(127.0.0.1:${MYSQL_PORT})/proofcycle_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true" \
	go test ./...

test-race:
	PROOFCYCLE_TEST_DSN="proof:proof@tcp(127.0.0.1:${MYSQL_PORT})/proofcycle_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true" \
	go test -race ./...

demo:
	BASE=http://127.0.0.1:${API_PORT} ./demo/demo.sh

fmt:
	gofmt -l -w .

vet:
	go vet ./...
