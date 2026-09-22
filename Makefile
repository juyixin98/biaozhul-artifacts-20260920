.PHONY: build generate test test-race vet fmt docker-up docker-down seed clean

build:
	go build ./...

generate:
	cd internal/db && sqlc generate -f sqlc.yaml

vet:
	go vet ./...

fmt:
	gofmt -w cmd internal tests

test:
	./scripts/run-tests.sh -count=1

test-race:
	./scripts/run-tests.sh -count=1 -race

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down -v

seed:
	docker compose run --rm seed
