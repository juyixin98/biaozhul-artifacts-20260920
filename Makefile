.PHONY: help up down build test test-integration fmt vet demo

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Start MySQL + app with docker compose
	docker compose up --build

down: ## Stop docker compose
	docker compose down

build: ## Build the server binary
	go build -o bin/geoterritory ./cmd/server

fmt: ## Format code
	gofmt -w .

vet: ## Static checks
	go vet ./...

test: ## Unit tests (no external dependencies)
	go test -count=1 ./geometry/... ./internal/engine/...

test-mysql-up: ## Start a throwaway MySQL on :33061 for integration tests
	docker run -d --name gt-test-mysql -e MYSQL_ROOT_PASSWORD=root \
		-e MYSQL_DATABASE=geoterritory_test -p 33061:3306 \
		mysql:8.4 --default-time-zone=+00:00
	@echo "waiting for mysql..."
	@for i in $$(seq 1 60); do \
		docker exec gt-test-mysql mysqladmin ping -uroot -proot >/dev/null 2>&1 && exit 0; \
		sleep 2; \
	done; echo "mysql did not become ready"; exit 1

test-mysql-down: ## Remove the test MySQL container
	docker rm -f gt-test-mysql

test-integration: ## Run all tests against the test MySQL on :33061
	TEST_MYSQL_DSN='root:root@tcp(127.0.0.1:33061)/geoterritory_test?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true' \
		go test -count=1 -race ./...

demo: ## Run the curl demo script (requires running stack + jq)
	./examples/demo.sh
