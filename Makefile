.PHONY: up down build test test-unit test-integration fmt vet logs

up:
	docker compose up --build -d

down:
	docker compose down

build:
	go build ./...

fmt:
	go fmt ./...

vet:
	go vet ./...

test-unit:
	go test ./internal/... -count=1

test-integration:
	go test ./tests/... -count=1 -v

test: test-unit test-integration

logs:
	docker compose logs -f server
