.PHONY: build test test-race run seed acceptance tidy clean

build:
	go build ./...

test:
	go test ./...

test-race:
	go test -race ./...

run:
	go run ./cmd/sensorhealth -seed

acceptance:
	bash scripts/acceptance.sh

tidy:
	go mod tidy

clean:
	rm -f sensorhealth.db sensorhealth.db-wal sensorhealth.db-shm /tmp/sensorhealth-server /tmp/sensorhealth-client
