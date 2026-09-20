.PHONY: all build run test test-race sqlc migrate fmt vet docker-up docker-down clean

DB_URL ?= postgres://sircc:sircc@127.0.0.1:5432/sircc?sslmode=disable
TEST_DB_URL ?= postgres://sircc:sircc@127.0.0.1:5432/sircc_test?sslmode=disable

all: build

build:
	go build -o bin/sircc ./cmd/sircc

run: build
	SIRCC_DATABASE_URL='$(DB_URL)' ./bin/sircc

# Regenerate sqlc code after editing internal/database/queries or migrations.
sqlc:
	sqlc generate

migrate: build
	SIRCC_DATABASE_URL='$(DB_URL)' ./bin/sircc -migrate

fmt:
	gofmt -w cmd internal tests

vet:
	go vet ./...

test:
	SIRCC_TEST_DATABASE_URL='$(TEST_DB_URL)' go test ./... -count=1

test-race:
	SIRCC_TEST_DATABASE_URL='$(TEST_DB_URL)' go test -race ./... -count=1

docker-up:
	sudo docker compose up --build -d

docker-down:
	sudo docker compose down -v

clean:
	rm -rf bin
