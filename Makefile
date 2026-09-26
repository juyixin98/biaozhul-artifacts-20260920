.PHONY: all build test cover race vet fmt accept clean run-demo

GO ?= go
BIN := bin

all: build

build:
	mkdir -p $(BIN)
	$(GO) build -o $(BIN)/idempotency-server ./cmd/server
	$(GO) build -o $(BIN)/auditserver ./cmd/auditserver
	$(GO) build -o $(BIN)/idemcheck ./cmd/client

test:
	$(GO) test -race -cover ./...

# 内部包（不含 cmd 装配代码）聚合覆盖率门槛 80%
cover:
	$(GO) test -race -coverprofile=coverage.out ./internal/...
	$(GO) tool cover -func=coverage.out | tail -1
	@$(GO) tool cover -func=coverage.out | tail -1 | awk '{f=$$3; sub("%","",f); if (f+0 < 80) exit 1}'

race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

# 端到端故障注入验收（自动编译、拉起子进程、输出 run/report.json）
accept:
	$(GO) run ./cmd/client -work ./run/acceptance -out ./run/report.json -keep

clean:
	rm -rf $(BIN) coverage.out data
