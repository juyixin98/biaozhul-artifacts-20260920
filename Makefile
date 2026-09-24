.PHONY: build test accept run clean

build:
	go build -o scalerd ./cmd/scalerd
	go build -o verify ./cmd/verify

test:
	go test ./...

accept:
	./scripts/accept.sh

run: build
	./scalerd -addr=:8080 -db=scaler.db

clean:
	rm -f scalerd verify scaler.db scaler.db-wal scaler.db-shm
