# Certificate Renewal Coordinator — build targets.
# Pure Go backend; the only external system for e2e is a kind (Kubernetes IN Docker) cluster.

REGISTRY ?= cert-renewal-controller
IMG      ?= $(REGISTRY):dev
KIND_CLUSTER ?= cert-renewal
NS       ?= default

.PHONY: help
help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS="(:.*## |## )"} {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$3}'

## ── local development ──────────────────────────────────────────────────────

.PHONY: build
build: ## Build manager and gentestca binaries into ./bin.
	go build -o bin/manager ./cmd/manager
	go build -o bin/gentestca ./cmd/gentestca

.PHONY: test
test: ## Run all unit + integration tests with the race detector.
	go test -race -count=1 ./...

.PHONY: vet
vet: ## go vet + gofmt check.
	go vet ./...
	@test -z "$$(gofmt -l .)" || { echo "gofmt needed on:"; gofmt -l .; exit 1; }

.PHONY: tidy
tidy: ## Tidy modules.
	go mod tidy

.PHONY: ca
ca: ## Generate a fresh sample test-CA Secret manifest under config/samples/generated.
	@mkdir -p config/samples/generated
	go run ./cmd/gentestca -namespace $(NS) -name test-ca -lifetime 720h \
	  > config/samples/generated/test-ca-secret.yaml

## ── kind e2e ───────────────────────────────────────────────────────────────

.PHONY: kind-up
kind-up: ## Create the kind cluster.
	kind get clusters | grep -q $(KIND_CLUSTER) || kind create cluster --name $(KIND_CLUSTER) --image kindest/node:v1.30.0

.PHONY: image
image: ## Build and load the controller image into kind.
	docker build --network=host -t $(IMG) .
	kind load docker-image $(IMG) --name $(KIND_CLUSTER)

.PHONY: deploy
deploy: ## Install CRD, RBAC, namespace and controller.
	kubectl apply -f config/namespace.yaml
	kubectl apply -f config/crd/
	kubectl apply -f config/rbac/
	kubectl apply -f config/manager/deployment.yaml
	kubectl rollout status deploy/cert-renewal-controller -n cert-renewal-system --timeout=90s

.PHONY: samples
samples: ca ## Generate the sample CA (if needed) and deploy sample Certificate(s).
	kubectl apply -f config/samples/generated/test-ca-secret.yaml
	kubectl apply -f config/samples/certificate.yaml
	kubectl apply -f config/samples/certificate-short-lived.yaml

.PHONY: e2e
e2e: kind-up image deploy samples ## Full bring-up; then run hack/e2e-acceptance.sh.
	./hack/e2e-acceptance.sh

.PHONY: kind-down
kind-down: ## Delete the kind cluster.
	kind delete cluster --name $(KIND_CLUSTER)
