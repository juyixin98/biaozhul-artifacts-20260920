.PHONY: build run migrate seed test test-race vet sqlc docker-up docker-seed fmt static

BIN := signalboard

build:
	CGO_ENABLED=0 go build -trimpath -o $(BIN) ./cmd/server

static:
	CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o $(BIN) ./cmd/server

run:
	go run ./cmd/server

migrate:
	go run ./cmd/server migrate

seed:
	go run ./cmd/server seed

sqlc:
	sqlc generate
	cp migrations/*.sql internal/migrate/sql/

fmt:
	gofmt -s -w .

vet:
	go vet ./...

test:
	go test ./...

test-race:
	go test -race -count=1 ./...

docker-up:
	docker compose up --build

docker-seed:
	docker compose run --rm seed
