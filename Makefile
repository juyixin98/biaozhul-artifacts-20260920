.PHONY: build test cover vet fmt run probe

build:
	go build ./...

test:
	go test ./... -count=1

cover:
	go test ./... -count=1 -cover

race:
	go test -race -count=1 ./...

vet:
	go vet ./...

fmt:
	gofmt -l .

run:
	go run ./cmd/server

probe:
	go run ./cmd/probe -fault flaky -flaky-times 2 -attempts 4 -fake-time
