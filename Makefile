.PHONY: build run test test-short test-db test-browser test-e2e migrate docker-build demo-seed tidy fmt

build:
	go build -o bin/sitevitals ./cmd/sitevitals

run:
	go run ./cmd/sitevitals serve

tidy:
	go mod tidy

fmt:
	gofmt -w .

test-short:
	go test -short ./...

test-browser:
	go test -count=1 ./internal/browser/

# Requires a MySQL 8 reachable via TEST_MYSQL_DSN (or testcontainers/Docker).
test-db:
	go test -count=1 ./internal/store/ ./internal/api/

test-e2e:
	TEST_MYSQL_DSN="$${TEST_MYSQL_DSN:-sv:sv@tcp(127.0.0.1:13399)/sitevitals_test?parseTime=true&loc=UTC}" \
	  go test -count=1 ./internal/worker/

test:
	go test -count=1 ./...

migrate:
	go run ./cmd/sitevitals migrate

demo-seed:
	go run ./cmd/sitevitals demo-seed --demo-origin "$${DEMO_ORIGIN:-http://127.0.0.1:8090}"

docker-build:
	docker build -t sitevitals:latest .

docker-up:
	docker compose up --build
