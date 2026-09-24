# Topology-aware GPU placement service — developer tasks.
# Go toolchain is the only dependency.

GO ?= go
BIN := bin/placementd
ADDR ?= :8080

.PHONY: all build run test test-race cover vet fmt fmt-check lint clean

all: build

build:
	$(GO) build -o $(BIN) ./cmd/placementd

run: build
	$(BIN) -addr $(ADDR)

test:
	$(GO) test ./...

test-race:
	$(GO) test -race ./...

cover:
	$(GO) test -coverprofile=coverage.out ./...
	$(GO) tool cover -func=coverage.out

vet:
	$(GO) vet ./...

fmt:
	$(GO)fmt -w .

fmt-check:
	@test -z "$$($(GO)fmt -l .)" || { echo "run 'make fmt'"; $(GO)fmt -l .; exit 1; }

# "lint" is go vet on purpose: no external linter dependency is required.
lint: vet

clean:
	rm -rf bin coverage.out
