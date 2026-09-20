.PHONY: up down build generate test test-int fmt vet logs psql demo

# Start Postgres + API with migrations applied automatically.
up:
	docker compose up --build -d

down:
	docker compose down

build:
	go build ./...

# Regenerate sqlc code after editing internal/db/query/*.sql
generate:
	sqlc generate

fmt:
	gofmt -w cmd internal

vet:
	go vet ./...

# Integration tests need a reachable PostgreSQL.
#   make test-int DB="postgres://sircc:sircc@localhost:5432/postgres?sslmode=disable"
DB ?= postgres://sircc:sircc@localhost:5432/postgres?sslmode=disable
test-int:
	TEST_DATABASE_URL="$(DB)" go test ./test/integration -count=1 -v

test: test-int

logs:
	docker compose logs -f api

psql:
	docker compose exec db psql -U sircc -d sircc

# End-to-end HTTP walkthrough against a running stack.
demo:
	./examples/demo.sh
