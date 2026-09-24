# merklekv — Anti-entropy Merkle synchronization (Go)

BINARY   := merklekv
PKG      := github.com/example/merklekv
GO       ?= go
GOFLAGS  ?=

.PHONY: all build test race bench vet fmt check clean vendor

all: build

build:
	$(GO) build $(GOFLAGS) -o bin/$(BINARY) ./cmd/$(BINARY)

test:
	$(GO) test $(GOFLAGS) -count=1 ./...

race:
	$(GO) test $(GOFLAGS) -race -count=1 ./...

bench:
	$(GO) test $(GOFLAGS) -run='^$$' -bench=. -benchmem ./...

vet:
	$(GO) vet ./...

fmt:
	@gofmt -l . | tee /dev/stderr | (! read)

# Vendor the (empty) third-party set so the build is reproducibly offline:
# `go mod vendor` succeeds and vendor/modules.txt records the module itself.
vendor:
	$(GO) mod vendor
	$(GO) mod verify

check: fmt vet test

clean:
	rm -rf bin
