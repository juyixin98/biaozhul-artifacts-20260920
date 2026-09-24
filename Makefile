IMG ?= cert-renewal-coordinator:latest
KIND_CLUSTER ?= cert-renewal
GO ?= go

.PHONY: help
help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

.PHONY: test
test: ## Run all unit tests with the race detector.
	$(GO) test ./... -count=1 -race

.PHONY: vet
vet: ## Run go vet.
	$(GO) vet ./...

.PHONY: build
build: ## Build the manager binary locally.
	$(GO) build -o bin/manager ./cmd/manager

.PHONY: docker-build
docker-build: ## Build the controller image.
	docker build -t $(IMG) .

.PHONY: kind-cluster
kind-cluster: ## Create the local kind cluster (uses cached kindest/node if present).
	kind get clusters | grep -qx $(KIND_CLUSTER) || kind create cluster --name $(KIND_CLUSTER)

.PHONY: kind-load
kind-load: docker-build ## Build and load the image into kind.
	kind load docker-image $(IMG) --name $(KIND_CLUSTER)

.PHONY: deploy
deploy: ## Install CRDs, RBAC and the controller into the current cluster.
	kubectl apply -f config/crd/bases/
	kubectl apply -f config/rbac/rbac.yaml
	kubectl apply -f config/manager/manager.yaml
	kubectl -n cert-renewal-system rollout status deploy/cert-renewal-coordinator --timeout=120s

.PHONY: undeploy
undeploy: ## Remove controller, RBAC and CRDs.
	kubectl delete -f config/manager/manager.yaml --ignore-not-found
	kubectl delete -f config/rbac/rbac.yaml --ignore-not-found
	kubectl delete -f config/crd/bases/ --ignore-not-found

.PHONY: sample-ca
sample-ca: ## Generate and apply the ephemeral test CA secret.
	$(GO) run ./hack/gen-test-ca test-ca default | kubectl apply -f -

.PHONY: samples
samples: ## Apply the example certificates.
	kubectl apply -f config/samples/

.PHONY: e2e
e2e: ## Run the kind end-to-end tests (needs KUBECONFIG + loaded image).
	$(GO) test -tags=e2e ./test/e2e -count=1 -v -timeout 15m

.PHONY: tidy
tidy:
	$(GO) mod tidy
