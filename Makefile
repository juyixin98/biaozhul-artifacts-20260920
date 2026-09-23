.PHONY: build run test test-race acceptance fmt vet clean

BIN_DIR := bin

build:
	go build -o $(BIN_DIR)/ws-server ./cmd/ws-server
	go build -o $(BIN_DIR)/ws-acceptance ./cmd/ws-acceptance

run:
	go run ./cmd/ws-server -addr 127.0.0.1:8080

test:
	go test ./... -count=1

test-race:
	go test -race ./... -count=1

test-stress:
	go test ./... -count=5

acceptance:
	go run ./cmd/ws-acceptance -depth 10 -fanout 2 -workers 1

fmt:
	gofmt -s -w .

vet:
	go vet ./...

clean:
	rm -rf $(BIN_DIR)
