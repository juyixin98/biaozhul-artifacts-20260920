# ximbox - cross-chain message inbox
#
# Targets assume a reachable PostgreSQL. Override with:
#   XIMBOX_DATABASE_URL=postgres://user:pass@host:5432/db?sslmode=disable

GO ?= go
BIN_DIR := bin
SERVER_BIN := $(BIN_DIR)/ximbox-server
FIXTURE_BIN := $(BIN_DIR)/ximbox-signfixture
DEV_DB_URL ?= postgres://ximbox:ximbox_dev_pwd@localhost:5432/ximbox?sslmode=disable
TEST_DB_URL ?= postgres://ximbox:ximbox_dev_pwd@localhost:5432/ximbox_test?sslmode=disable

.PHONY: all build test test-race vet fmt run demo demo-crash clean db-create fixtures tidy

all: build

build: $(SERVER_BIN) $(FIXTURE_BIN)

$(SERVER_BIN): $(shell find cmd internal -name '*.go' | grep -v _test)
	@mkdir -p $(BIN_DIR)
	$(GO) build -o $@ ./cmd/server

$(FIXTURE_BIN): $(shell find cmd internal -name '*.go' | grep -v _test)
	@mkdir -p $(BIN_DIR)
	$(GO) build -o $@ ./cmd/signfixture

# Create the local role + databases used by defaults (Debian/Ubuntu pg_peer).
db-create:
	sudo -u postgres psql -v ON_ERROR_STOP=1 -c \
	  "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ximbox') THEN CREATE ROLE ximbox LOGIN PASSWORD 'ximbox_dev_pwd'; END IF; END \$\$;"
	-sudo -u postgres createdb -O ximbox ximbox
	-sudo -u postgres createdb -O ximbox ximbox_test

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

tidy:
	$(GO) mod tidy

test:
	XIMBOX_TEST_DATABASE_URL=$(TEST_DB_URL) $(GO) test ./...

test-race:
	XIMBOX_TEST_DATABASE_URL=$(TEST_DB_URL) $(GO) test -race ./...

run: $(SERVER_BIN)
	XIMBOX_DATABASE_URL=$(DEV_DB_URL) XIMBOX_HTTP_ADDR=:8080 ./$(SERVER_BIN)

fixtures: $(FIXTURE_BIN)
	./$(FIXTURE_BIN) keys

demo: build
	bash examples/demo_walkthrough.sh

demo-crash: build
	bash examples/demo_crash_recovery.sh

clean:
	rm -rf $(BIN_DIR)
