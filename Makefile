GO ?= go

.PHONY: build test test-race vet fmt demo tidy clean

build:
	$(GO) build ./...

test:
	$(GO) test ./...

test-race:
	$(GO) test -race -count=1 ./...

vet:
	$(GO) vet ./...

fmt:
	$(GO)fmt -w .

tidy:
	$(GO) mod tidy

demo: build
	./scripts/demo.sh

clean:
	rm -f orset-server orset-gc
