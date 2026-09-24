# mirrorsec —— 镜像离线安全策略（纯后端）

所有命令默认使用随仓库锁定的 `vendor/` 依赖，可离线构建。

.PHONY: all build test race vet fmt examples run acceptance clean

all: build

build:
	go build -mod=vendor -o bin/admissiond ./cmd/admissiond
	go build -mod=vendor -o bin/mirrorctl ./cmd/mirrorctl

test:
	go test -mod=vendor -count=1 ./...

race:
	go test -mod=vendor -race -count=1 ./...

vet:
	go vet -mod=vendor ./...

fmt:
	gofmt -l -w .

# 重新生成示例密钥与证据（会覆盖 examples/ 与 internal/policy/allowlist.json）。
# 注意：新密钥会改变示例签名/验签结果，属正常；测试不依赖 examples 目录。
examples: build
	./bin/mirrorctl gen-examples -out examples

run: build
	./bin/admissiond -addr 127.0.0.1:18080 -trust examples/keys/trust.json -reports data/reports

# 一键端到端验收（可 PORT=xxxx make acceptance 换端口）。
acceptance:
	bash scripts/acceptance.sh

clean:
	rm -rf bin/ data/
