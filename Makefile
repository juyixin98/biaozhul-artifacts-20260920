.PHONY: all build sqlc migrate-sync test test-unit test-integration fmt vet docker-up docker-seed docker-down clean

all: build

build:
	go build -o bin/costlens-api  ./cmd/api
	go build -o bin/costlens-seed ./cmd/seed

# Regenerate sqlc code after editing internal/db/queries or migrations.
sqlc:
	sqlc generate
	$(MAKE) migrate-sync

# The API embeds the init migration; keep the embedded copy identical.
migrate-sync:
	cp migrations/0001_init.sql internal/migrate/0001_init.sql

test: test-unit test-integration

test-unit:
	go test ./internal/... -count=1

# Requires a reachable PostgreSQL; defaults to the docker-compose db.
# Override with COSTLENS_TEST_DATABASE_URL=postgres://user:pass@host:port/postgres?sslmode=disable
test-integration:
	COSTLENS_TEST_DATABASE_URL=$${COSTLENS_TEST_DATABASE_URL:-postgres://costlens:costlens@localhost:55432/postgres?sslmode=disable} \
		go test ./test/... -count=1

fmt:
	gofmt -l -w .

vet:
	go vet ./...

docker-up:
	docker compose up -d --build

docker-seed:
	docker compose run --rm seed

docker-down:
	docker compose down

clean:
	rm -rf bin
