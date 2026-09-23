.PHONY: run test race vet fmt build tidy acceptance clean

ADDR ?= :7379

run:
	go run . -addr $(ADDR)

build:
	go build -o bin/respd .

test:
	go test -timeout 60s ./...

race:
	go test -race -timeout 120s ./...

vet:
	go vet ./...

fmt:
	gofmt -l -w .

tidy:
	go mod tidy
	go mod verify

acceptance:
	python3 examples/acceptance.py

clean:
	rm -rf bin
