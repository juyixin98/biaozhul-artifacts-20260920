.PHONY: all build test race vet fmt scenarios run clean

all: build

build:
	go build ./...

test:
	go test ./...

race:
	go test -race ./...

vet:
	go vet ./...

fmt:
	gofmt -w .

scenarios:
	go run ./cmd/genscenarios -out scenarios

run:
	go run ./cmd/raftlab -addr :8080

clean:
	rm -f raftlab
