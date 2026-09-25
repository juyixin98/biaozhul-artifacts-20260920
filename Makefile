GO ?= go

.PHONY: build test vet fmt verify run clean

build:
	$(GO) build ./...

test:
	$(GO) test -race -cover ./...

vet:
	$(GO) vet ./...

fmt:
	@out=$$(gofmt -l .); if [ -n "$$out" ]; then echo "$$out"; exit 1; fi; echo "all formatted"

verify:
	$(GO) run ./cmd/verify

run:
	$(GO) run ./cmd/server -addr :8080 -data ./data/snapshot.json

clean:
	rm -rf data/
