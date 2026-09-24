# tztrig development targets
GO ?= go
GOFLAGS_LOCAL := GOTOOLCHAIN=local

.PHONY: all build test race cover zoneinfo run clean fmt vet

all: build

build:
	$(GOFLAGS_LOCAL) $(GO) build -trimpath -o bin/tztrig ./cmd/tztrig

test:
	$(GOFLAGS_LOCAL) $(GO) test -count=1 ./...

race:
	$(GOFLAGS_LOCAL) $(GO) test -race -count=1 ./...

cover:
	$(GOFLAGS_LOCAL) $(GO) test -coverprofile=coverage.out ./...
	$(GO) tool cover -func=coverage.out | tail -1

# Rebuild the embedded IANA database from the pinned release.
zoneinfo:
	./scripts/build-zoneinfo.sh

fmt:
	$(GO) fmt ./...

vet:
	$(GO) vet ./...

run: build
	./bin/tztrig

clean:
	rm -rf bin coverage.out data
