.PHONY: all build deps migrate examples serve test race verify smoke ingest clean

BIN := forkindexer
PKG := ./cmd/forkindexer
DSN ?= postgres:///forkindexer?host=/var/run/postgresql
F ?= examples/stream1_three_forks.ndjson
ADDR ?= 127.0.0.1:8080

all: build

build:
	go build -o $(BIN) $(PKG)

deps:
	go mod download

migrate:
	DATABASE_URL='$(DSN)' go run $(PKG) migrate

examples:
	go run $(PKG) gen-examples examples

serve:
	DATABASE_URL='$(DSN)' go run $(PKG) serve -addr $(ADDR)

test:
	go test ./...

race:
	go test -race ./...

verify:
	DATABASE_URL='$(DSN)' go run $(PKG) verify

ingest:
	DATABASE_URL='$(DSN)' go run $(PKG) ingest $(F)

smoke:
	python3 scripts/smoke.py http://$(ADDR) $(F)

clean:
	rm -f $(BIN)
