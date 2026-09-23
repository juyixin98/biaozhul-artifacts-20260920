# 可撤销凭证索引 — Makefile
# 合成测试身份；本机 PostgreSQL 或 docker compose 二选一。

DB_URL ?= postgres://vc_test:vc_test@localhost:5432/vc_index?sslmode=disable
TEST_DB_URL ?= postgres://vc_test:vc_test@localhost:5432/vc_index_test?sslmode=disable
ADDR ?= :8090

.PHONY: build run test test-db test-all vet fmt devdb docker-db accept tidy

build:
	go build -o bin/vci-server ./cmd/server

run:
	DATABASE_URL='$(DB_URL)' VCI_ADDR='$(ADDR)' go run ./cmd/server

test:
	go test -race -count=1 ./internal/...

test-db:
	RUN_DB_TESTS=1 TEST_DATABASE_URL='$(TEST_DB_URL)' go test -race -count=1 ./test/...

test-all: test test-db

vet:
	go vet ./...
	gofmt -l . | grep . && { echo "gofmt: unformatted files above"; exit 1; } || true

fmt:
	gofmt -w .

devdb:
	sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='vc_test'" | grep -q 1 \
	  || sudo -u postgres psql -c "CREATE ROLE vc_test LOGIN PASSWORD 'vc_test' CREATEDB;"
	sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='vc_index'" | grep -q 1 \
	  || sudo -u postgres psql -c "CREATE DATABASE vc_index OWNER vc_test;"
	sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='vc_index_test'" | grep -q 1 \
	  || sudo -u postgres psql -c "CREATE DATABASE vc_index_test OWNER vc_test;"

docker-db:
	docker compose up -d db

accept:
	BASE_URL='$(BASE_URL)' examples/walkthrough.sh

tidy:
	go mod tidy
