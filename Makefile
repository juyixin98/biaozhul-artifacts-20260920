DATABASE_URL ?= postgres://revcred:revcred@localhost:55433/revcred?sslmode=disable
export DATABASE_URL
export TEST_DATABASE_URL ?= $(DATABASE_URL)

.PHONY: up down wait-db run test acceptance build

up:            ## 启动 PostgreSQL（docker compose）
	docker compose up -d postgres
	$(MAKE) wait-db

wait-db:       ## 等待数据库就绪
	@until docker compose exec -T postgres pg_isready -U revcred -d revcred >/dev/null 2>&1; do sleep 0.5; done
	@echo "postgres is ready"

down:          ## 停止并删除数据库容器与数据卷
	docker compose down -v

build:         ## 编译
	go build ./...

run:           ## 启动服务（自动应用迁移）
	go run ./cmd/server

test:          ## 运行全部自动化测试（需要数据库）
	go test ./... -count=1

acceptance:    ## 运行端到端验收脚本（需要服务已在 :8080 运行）
	./examples/acceptance.sh
