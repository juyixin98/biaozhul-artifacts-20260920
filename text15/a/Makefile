.PHONY: build test run docker-up docker-down demo

build:
	go build ./...

test:
	go test ./...

run:
	go run ./cmd/server

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down -v

demo:
	./scripts/demo.sh
