.PHONY: help db db-test migrate build run test test-race demo fmt vet

help:
	@echo "make db         create local Postgres role + databases"
	@echo "make build      compile the server"
	@echo "make run        run the server on 127.0.0.1:8080"
	@echo "make test       run all tests (needs registry_gc_test)"
	@echo "make test-race  run tests under the race detector"
	@echo "make demo       run the end-to-end acceptance demo (server must be up)"
	@echo "make fmt vet    format and vet"

db:
	sudo -u postgres psql -c "CREATE ROLE gcuser LOGIN PASSWORD 'gcpass';" || true
	sudo -u postgres psql -c "CREATE DATABASE registry_gc OWNER gcuser;" || true
	sudo -u postgres psql -c "CREATE DATABASE registry_gc_test OWNER gcuser;" || true

build:
	go build -o bin/registry-gc ./cmd/server

run: build
	./bin/registry-gc

test:
	go test ./... -count=1 -timeout 180s

test-race:
	go test ./... -race -count=1 -timeout 240s

demo:
	bash scripts/demo.sh

fmt:
	go fmt ./...

vet:
	go vet ./...
