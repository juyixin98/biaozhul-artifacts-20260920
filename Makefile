# Modbus Write Receipt — build / test / acceptance
# Pure Go (SQLite via modernc.org/sqlite, CGO not required).

GO ?= go
BIN := bin
ADDR ?= 127.0.0.1:1502
DB ?= receipts.db
KEY ?= hmac.key

.PHONY: all build test test-race vet fmt run demo verify clean

all: build

build:
	$(GO) build -o $(BIN)/modbus-server ./cmd/modbus-server
	$(GO) build -o $(BIN)/modbus-replay ./cmd/modbus-replay

test:
	$(GO) test ./... -count=1

test-race:
	$(GO) test -race ./... -count=1

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

# Local run with a throwaway key (dev only).
run: build
	@test -s $(KEY) || printf 'dev-only-key-change-me' > $(KEY)
	$(BIN)/modbus-server -listen $(ADDR) -db $(DB) -registers 100 -key-file $(KEY)

# End-to-end acceptance: boots a real server, runs every example scenario,
# verifies the receipt hash chain, and tears the server down.
demo: build
	@./scripts/acceptance.sh

verify: build
	$(BIN)/modbus-replay verify -db $(DB) -key-file $(KEY)

clean:
	rm -rf $(BIN) $(DB) $(DB)-wal $(DB)-shm $(KEY)
