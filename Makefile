.PHONY: build test test-race fmt vet lint run docker-up docker-down migrate-embed

BIN := bin/synapticgod

build:
	go build -o $(BIN) ./cmd/server

run: build
	$(BIN)

fmt:
	gofmt -s -w .

vet:
	go vet ./...

# Integration tests need PostgreSQL; point SYN_TEST_DATABASE_URL at an empty
# database the current role can create/drop siblings of (each test run makes
# its own throw-away database).
test:
	go test -count=1 ./...

test-race:
	go test -count=1 -race ./...

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v
