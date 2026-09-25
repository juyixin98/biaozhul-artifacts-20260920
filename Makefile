# Log template online clustering — build/test/run.
# Standard Go toolchain only, no external dependencies.

BIN_DIR   := bin
SERVER    := $(BIN_DIR)/logcluster
EVAL      := $(BIN_DIR)/eval
GO        := go

.PHONY: all build test test-race bench vet fmt eval demo clean

all: build

build: $(SERVER) $(EVAL)

$(SERVER): $(wildcard cmd/logcluster/*.go) go.mod $(wildcard internal/**/*.go)
	$(GO) build -o $@ ./cmd/logcluster

$(EVAL): $(wildcard cmd/eval/*.go) go.mod $(wildcard internal/**/*.go)
	$(GO) build -o $@ ./cmd/eval

test:
	$(GO) test ./... -count=1

test-race:
	$(GO) test -race ./... -count=1

bench:
	$(GO) test ./... -run=^$ -bench=. -benchtime=200x

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

# Labeled acceptance gate (exits non-zero if purity/recall < 1.0).
eval: build
	$(EVAL)

eval-json: build
	$(EVAL) --json

# End-to-end HTTP demo (LRU eviction, queries, snapshot restart).
demo: build
	./scripts/demo.sh

clean:
	rm -rf $(BIN_DIR) data
