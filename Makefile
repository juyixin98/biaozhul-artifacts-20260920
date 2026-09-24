# Resource Quota Reservation — developer targets
#
# Toolchain (versions are those validated on this machine; pin by PATH):
#   go 1.22.x, controller-gen v0.16.5, setup-envtest (k8s 1.30.x assets),
#   kind v0.23.x, kubectl v1.30.x, docker, openssl, jq

GOOS ?= $(shell go env GOOS)
GOARCH ?= $(shell go env GOARCH)
CONTROLLER_GEN ?= $(shell go env GOPATH)/bin/controller-gen
ENVTEST ?= $(shell go env GOPATH)/bin/setup-envtest
K8S_VERSION ?= 1.30.x
IMG ?= quota-controller:dev

.PHONY: all
all: build

.PHONY: build
build: ## Build the manager binary for the host
	go build -o bin/manager ./cmd/manager

.PHONY: build-linux
build-linux: ## Static linux/amd64 build used inside the distroless image
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
	  go build -trimpath -ldflags '-s -w' -o bin/manager ./cmd/manager

.PHONY: run
run: ## Run the controller out-of-cluster against $KUBECONFIG
	go run ./cmd/manager

.PHONY: generate
generate: ## Regenerate deepcopy funcs and CRDs from API markers
	$(CONTROLLER_GEN) object:headerFile=/dev/null paths=./api/...
	$(CONTROLLER_GEN) crd paths=./api/... output:crd:dir=./config/crd
	@cp config/webhook/manifests.yaml config/webhook/manifests-envtest.yaml
	@sed -i '/caBundle: __CA_BUNDLE__/d' config/webhook/manifests-envtest.yaml

.PHONY: test
test: ## Fast unit tests (ledger + controller reconcile)
	go test -count=1 ./internal/...

.PHONY: envtest-assets
envtest-assets: ## Download kube-apiserver/etcd used by integration tests
	$(ENVTEST) use $(K8S_VERSION)

.PHONY: test-integration
test-integration: ## envtest integration suite (real apiserver + admission)
	KUBEBUILDER_ASSETS="$($(ENVTEST) use -i -p path $(K8S_VERSION))" \
	  go test -count=1 -timeout 10m -v ./test/integration/

.PHONY: test-all
test-all: test test-integration ## Everything

.PHONY: certs
certs: ## Generate webhook CA + serving cert into hack/_output
	OUTDIR=hack/_output bash hack/gen-certs.sh

.PHONY: docker-build
docker-build: build-linux ## Package the host-built binary (no build network)
	docker build -t $(IMG) .

.PHONY: kind-cluster
kind-cluster: ## Create the local kind cluster
	kind create cluster --name quota-reservation \
	  --image kindest/node:v1.30.10 --config hack/kind-config.yaml

.PHONY: deploy
deploy: ## Full local deploy onto kind
	bash hack/deploy.sh

.PHONY: e2e
e2e: ## Acceptance scenarios against the live kind cluster
	bash hack/e2e-acceptance.sh

.PHONY: teardown
teardown: ## Delete the kind cluster and generated certs
	bash hack/destroy.sh

.PHONY: tidy
tidy:
	go mod tidy

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
