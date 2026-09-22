.PHONY: up build run test test-integration seed sample burst down logs fmt vet tidy

# 如本机端口被占用，可覆盖：make up MYSQL_PORT=3307 API_PORT=18092
MYSQL_PORT ?= 3306
API_PORT ?= 8080

up:
	MYSQL_PORT=$(MYSQL_PORT) API_PORT=$(API_PORT) docker compose up -d --build

build:
	go build ./...

run:
	go run ./cmd/server

test:
	go test ./internal/...

# 需要先启动 MySQL，例如：MYSQL_PORT=3307 docker compose up -d mysql
test-integration:
	AG_RUN_INTEGRATION=1 \
	AG_TEST_ADMIN_DSN="root:rootpw_change_me@tcp(127.0.0.1:$(MYSQL_PORT))/?charset=utf8mb4&parseTime=true&loc=UTC" \
	go test -p 1 -count=1 ./tests/...

sample:
	API_URL=http://127.0.0.1:$(API_PORT) go run ./cmd/seed-events file samples/events.sample.json

burst:
	API_URL=http://127.0.0.1:$(API_PORT) go run ./cmd/seed-events gen --mode burst \
		--employee-email bob.li@example.com --count 51

zscore:
	API_URL=http://127.0.0.1:$(API_PORT) go run ./cmd/seed-events gen --mode zscore \
		--employee-email bob.li@example.com --baseline 3 --today 20 --days 29

down:
	docker compose down

logs:
	docker compose logs -f api

fmt:
	gofmt -l -w .

vet:
	go vet ./...

tidy:
	go mod tidy
