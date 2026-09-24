.PHONY: all build test test-race demo clean fmt vet

GO ?= go
BIN := bin/tpc

all: build

build: $(BIN)

$(BIN): $(shell find . -name '*.go' -not -path './bin/*') go.mod
	$(GO) build -o $(BIN) ./cmd/tpc

test:
	$(GO) test -count=1 ./...

test-race:
	$(GO) test -race -count=1 ./...

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

demo: build
	./scripts/demo.sh

clean:
	rm -rf bin demo-data
