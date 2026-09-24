# CRD migration compatibility verification — task runner.
#
# Targets are self-contained: `make test` downloads envtest binaries via
# setup-envtest on first run. Override the Kubernetes control-plane version
# with K8S_VERSION=1.31.0.

K8S_VERSION ?= 1.31.0
SETUP_ENVTEST := $(shell go env GOPATH)/bin/setup-envtest
export KUBEBUILDER_ASSETS := $(shell $(SETUP_ENVTEST) use $(K8S_VERSION) -p path 2>/dev/null)

.PHONY: help build test unit-test integration-test acceptance dev stop-dev tidy lint clean fixtures

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

build: ## Build all commands
	go build ./...

tidy: ## Lock and tidy dependencies
	go mod tidy

unit-test: ## Fast tests: converter + webhook handlers (no cluster)
	go test ./internal/... -count=1

integration-test: ## envtest end-to-end suite (real apiserver+etcd)
	@command -v setup-envtest >/dev/null 2>&1 || $(MAKE) install-tools
	go test ./test/integration/... -count=1

test: unit-test integration-test ## Full test suite

slow-test: ## Include the 30s conversion-timeout e2e test
	RUN_SLOW_TESTS=1 go test ./test/integration/... -count=1 -run TestConversionTimeoutEndToEnd

install-tools: ## Install setup-envtest (envtest binary manager)
	go install sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.19

dev: ## Start the local environment (apiserver+etcd+webhooks), foreground
	@command -v setup-envtest >/dev/null 2>&1 || $(MAKE) install-tools
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use $(K8S_VERSION) -p path)" go run ./cmd/dev-env

acceptance: ## Automated acceptance script (builds dev-env and exercises it)
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use $(K8S_VERSION) -p path)" bash hack/acceptance.sh

lint: ## go vet + gofmt check
	go vet ./...
	@test -z "$$(gofmt -l .)" || (echo "gofmt needed on:"; gofmt -l .; exit 1)

clean: ## Remove built artifacts
	rm -f migration-records.jsonl /tmp/dev-env
