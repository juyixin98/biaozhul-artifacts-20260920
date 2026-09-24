.PHONY: all proto build test test-race run demo clean initdb seed tidy

PROTO_DIR := proto
GEN_DIR   := gen

all: proto build

proto:
	protoc -I $(PROTO_DIR) \
	  --go_out=$(GEN_DIR) --go_opt=module=github.com/example/compgw/gen \
	  --go-grpc_out=$(GEN_DIR) --go-grpc_opt=module=github.com/example/compgw/gen \
	  $(PROTO_DIR)/telemetry/v1/telemetry.proto \
	  $(PROTO_DIR)/telemetry/v2/telemetry.proto \
	  $(PROTO_DIR)/gateway/v1/gateway.proto

build:
	go build ./...
	go build -o bin/gateway ./cmd/gateway
	go build -o bin/demo ./cmd/demo

tidy:
	go mod tidy

initdb:
	bash scripts/initdb.sh

run:
	go run ./cmd/gateway

demo:
	go run ./cmd/demo v1-to-v2

examples:
	go run ./cmd/demo gen-examples

test:
	go test -count=1 ./...

test-race:
	go test -race -count=1 ./...

vet:
	go vet ./...

clean:
	rm -rf bin
