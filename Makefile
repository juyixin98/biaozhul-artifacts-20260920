.PHONY: build test test-race vet fmt demo clean run

# 锁定工具链：使用本机 Go（go.mod 声明 go1.23 / toolchain go1.23.4），不自动下载切换。
export GOTOOLCHAIN := local

build:
	go build -mod=vendor -o bin/server ./cmd/server

run:
	go run -mod=vendor ./cmd/server -addr :8080

test:
	go test -mod=vendor ./...

test-race:
	go test -mod=vendor -race ./...

vet:
	go vet -mod=vendor ./...

fmt:
	gofmt -w .

demo:
	./scripts/demo.sh

clean:
	rm -rf bin
