.PHONY: all build test test-race vet fmt clean run-fake run-proxy

GO ?= go

all: vet test build

build:
	$(GO) build -o bin/dnsproxy ./cmd/dnsproxy
	$(GO) build -o bin/fakedns  ./cmd/fakedns

test:
	$(GO) test -count=1 ./...

test-race:
	$(GO) test -race -count=1 ./...

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

run-fake: build
	./bin/fakedns -listen 127.0.0.1:5354

run-proxy: build
	./bin/dnsproxy -listen 127.0.0.1:8080 -upstream 127.0.0.1:5354

clean:
	rm -rf bin
