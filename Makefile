.PHONY: up down build test test-race run demo fmt vet tidy clean

DATABASE_URL ?= postgres://deadlock:deadlock@localhost:55432/deadlock?sslmode=disable

up:
	docker compose up -d
	@echo "waiting for postgres..."
	@for i in $$(seq 1 30); do \
	  docker exec deadlock-pg pg_isready -U deadlock -d deadlock >/dev/null 2>&1 && break; \
	  sleep 1; \
	done

down:
	docker compose down -v

build:
	go build ./...

fmt:
	gofmt -l -w .

vet:
	go vet ./...

tidy:
	go mod tidy

test: up
	go test -count=1 ./...

test-race: up
	go test -race -count=1 ./...

run: up
	DATABASE_URL="$(DATABASE_URL)" go run ./cmd/deadlockd

demo: up
	@# start server in background, run the walkthrough, then stop it
	DATABASE_URL="$(DATABASE_URL)" go run ./cmd/deadlockd & echo $$! > /tmp/deadlockd.pid; \
	sleep 1.5; ./examples/demo.sh; kill $$(cat /tmp/deadlockd.pid) 2>/dev/null || true

clean: down
	rm -f /tmp/deadlockd.pid
