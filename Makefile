.PHONY: all build test test-race vet fmt demo run clean

GO ?= go
BIN := bin/hlc-server

all: build

build:
	$(GO) build -o $(BIN) ./cmd/hlc-server

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

test:
	$(GO) test -count=1 ./...

test-race:
	$(GO) test -race -count=1 ./...

run: build
	$(BIN)

demo: build
	./scripts/demo.sh

clean:
	rm -rf bin
