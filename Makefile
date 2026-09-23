BIN := bin
export PATH := $(HOME)/go/bin:$(PATH)

.PHONY: all proto build test test-integration run db-up db-migrate acceptance clean

all: build

# Regenerate Go code from proto/ (requires protoc, protoc-gen-go, protoc-gen-go-grpc).
proto:
	protoc -I proto \
	  --go_out=gen --go_opt=paths=source_relative \
	  --go-grpc_out=gen --go-grpc_opt=paths=source_relative \
	  proto/telemetry/v1/telemetry.proto \
	  proto/telemetry/v2/telemetry.proto \
	  proto/gateway/v1/gateway.proto

build:
	go build -o $(BIN)/gateway ./cmd/gateway
	go build -o $(BIN)/clientv1 ./cmd/clientv1
	go build -o $(BIN)/clientv2 ./cmd/clientv2
	go build -o $(BIN)/gwctl ./cmd/gwctl

# Unit + in-process (bufconn) tests; no database needed.
test:
	go vet ./...
	go test ./...

# PostgreSQL-backed integration tests (requires a database).
test-integration:
	GATEWAY_TEST_DSN="$${GATEWAY_TEST_DSN:-postgres://telegw:telegw@localhost:5432/telegw_test}" \
	  go test ./internal/store/ -v

# Optional Postgres in Docker (a local server works too; just set GATEWAY_DATABASE_DSN).
db-up:
	docker run -d --name telegw-pg -p 5432:5432 \
	  -e POSTGRES_USER=telegw -e POSTGRES_PASSWORD=telegw -e POSTGRES_DB=telegw \
	  postgres:16-alpine || docker start telegw-pg

db-migrate:
	psql "$${GATEWAY_DATABASE_DSN:-postgres://telegw:telegw@localhost:5432/telegw}" \
	  -f migrations/0001_init.sql

run: build
	GATEWAY_DATABASE_DSN="$${GATEWAY_DATABASE_DSN:-postgres://telegw:telegw@localhost:5432/telegw}" \
	  ./$(BIN)/gateway

# End-to-end acceptance against a running gateway on 127.0.0.1:50051.
acceptance: build
	./scripts/acceptance.sh

clean:
	rm -rf $(BIN)
