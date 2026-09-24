.PHONY: all build test race cover fmt vet fixtures acceptance clean

all: build

build:
	go build ./...

test:
	go test ./...

race:
	go test -race ./...

cover:
	go test ./internal/... -cover

fmt:
	gofmt -w .

vet:
	go vet ./...

# Generate deterministic example OCI repositories under ./registry
fixtures:
	go run ./cmd/genfixtures -registry ./registry

# Full end-to-end acceptance (builds, fixtures, live server, assertions)
acceptance:
	./scripts/acceptance.sh

clean:
	rm -rf data
