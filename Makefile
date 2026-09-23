SHELL := /bin/bash

CONTROLLER_GEN := $(CURDIR)/bin/controller-gen
CLUSTER ?= p090b-e2e
KIND_IMAGE ?= kindest/node:v1.30.10

.PHONY: all
all: build

.PHONY: generate
generate: $(CONTROLLER_GEN) ## Deepcopy + CRD + RBAC manifests
	$(CONTROLLER_GEN) object paths=./api/...
	$(CONTROLLER_GEN) crd rbac:roleName=config-distributor \
		paths=./api/... paths=./internal/... \
		output:crd:dir=config/crd/bases output:rbac:dir=config/rbac

$(CONTROLLER_GEN):
	GOBIN=$(CURDIR)/bin go install sigs.k8s.io/controller-tools/cmd/controller-gen@v0.16.5

.PHONY: build
build: ## Compile the manager
	go build ./...

.PHONY: test
test: ## Unit tests (fake client; no cluster needed)
	go test ./... -count=1

.PHONY: vet
vet:
	go vet ./...

.PHONY: run
run: ## Run the controller against the current kubeconfig context
	go run ./cmd/manager

.PHONY: e2e
e2e: ## Full end-to-end demo on a fresh kind cluster (evidence in artifacts/)
	hack/e2e.sh

.PHONY: e2e-clean
e2e-clean: ## Delete the e2e kind cluster
	kind delete cluster --name $(CLUSTER) || true
