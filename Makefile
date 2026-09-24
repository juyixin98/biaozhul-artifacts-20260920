.PHONY: help build test test-race vet fmt docker-build kind-up deploy e2e kind-down tidy

CLUSTER ?= config-dedup
IMAGE   ?= config-distributor:dev

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "%-16s %s\n", $$1, $$2}'

build: ## Compile the manager binary
	CGO_ENABLED=0 go build -o bin/manager ./cmd/manager

test: ## Run unit tests
	go test ./... -count=1

test-race: ## Run unit tests with the race detector
	go test -race ./... -count=1

vet: ## go vet
	go vet ./...

fmt: ## Format the Go code
	gofmt -s -w . api cmd internal

tidy: ## Tidy modules
	go mod tidy

docker-build: ## Build the manager container image
	docker build -t $(IMAGE) .

kind-up: ## Create the demo kind cluster
	hack/deploy.sh

deploy: kind-up ## Alias for full local deployment

e2e: ## Run the end-to-end demo (writes evidence under test/evidence/)
	hack/e2e.sh

kind-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)
