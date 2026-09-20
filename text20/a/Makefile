.PHONY: all build sqlc vet fmt test race seed run docker-build up down

BIN := bin
export CGO_ENABLED := 0

all: build

build:
	go build -trimpath -ldflags="-s -w" -o $(BIN)/server ./cmd/server
	go build -trimpath -ldflags="-s -w" -o $(BIN)/seed   ./cmd/seed

sqlc:
	sqlc generate

vet:
	go vet ./...

fmt:
	gofmt -w internal cmd

# TEST_DATABASE_URL must point at a reachable PostgreSQL; tests skip without it.
test:
	go test -count=1 ./...

race:
	go test -count=1 -race ./...

seed:
	go run ./cmd/seed

run:
	go run ./cmd/server

docker-build:
	docker build -t signalboard:latest .

up:
	docker compose up --build

down:
	docker compose down
