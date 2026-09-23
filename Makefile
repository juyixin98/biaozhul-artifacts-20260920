# Snapshot Controller — Makefile
#
# Common targets:
#   make tools        install controller-gen / setup-envtest / kind / kubectl into ./bin
#   make generate     regenerate deepcopy + CRD + RBAC
#   make build        compile the manager binary
#   make test         run envtest-based unit/integration tests
#   make docker-build build controller + worker container images
#   make e2e          full local kind end-to-end (cluster, images, scenarios)
#   make e2e-down     tear down the kind cluster

GOOS        ?= $(shell go env GOOS)
GOARCH      ?= $(shell go env GOARCH)
BIN_DIR      := $(abspath bin)
ENVTEST_DIR  := $(BIN_DIR)/envtest
KIND_CLUSTER ?= snapshot-e2e
KIND_VERSION ?= v0.24.0
KUBECTL_VER  ?= v1.31.0
K8S_VERSION  ?= 1.31.x

CONTROLLER_GEN := $(BIN_DIR)/controller-gen
SETUP_ENVTEST  := $(BIN_DIR)/setup-envtest
KIND           := $(BIN_DIR)/kind
KUBECTL        := $(BIN_DIR)/kubectl

export PATH := $(BIN_DIR):$(PATH)

.PHONY: all tools generate fmt vet build test docker-build kind-load e2e e2e-down clean

all: generate build

##@ Toolchain

tools: $(CONTROLLER_GEN) $(SETUP_ENVTEST) $(KIND) $(KUBECTL)

$(CONTROLLER_GEN):
	go build -o $@ sigs.k8s.io/controller-tools/cmd/controller-gen

$(SETUP_ENVTEST):
	go build -o $@ sigs.k8s.io/controller-runtime/tools/setup-envtest

$(KIND):
	mkdir -p $(BIN_DIR)
	curl -fsSL -o $@ https://kind.sigs.k8s.io/dl/$(KIND_VERSION)/kind-$(GOOS)-$(GOARCH)
	chmod +x $@

$(KUBECTL):
	mkdir -p $(BIN_DIR)
	curl -fsSL -o $@ https://dl.k8s.io/release/$(KUBECTL_VER)/bin/$(GOOS)/$(GOARCH)/kubectl
	chmod +x $@

##@ Code generation

generate: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) object:headerFile="hack/boilerplate.go.txt" paths="./api/..."
	$(CONTROLLER_GEN) crd paths="./api/..." output:crd:artifacts:config=config/crd/bases
	$(CONTROLLER_GEN) rbac:roleName=snapshot-manager-role paths="./internal/controller/..." output:rbac:artifacts:config=config/rbac

fmt:
	go fmt ./...

vet:
	go vet ./...

##@ Build

build:
	CGO_ENABLED=0 go build -o $(BIN_DIR)/manager ./cmd/manager

##@ Tests

ENVTEST_ASSETS := $(shell $(SETUP_ENVTEST) use $(K8S_VERSION) --bin-dir $(ENVTEST_DIR) -p path 2>/dev/null)

test: $(SETUP_ENVTEST) generate
	KUBEBUILDER_ASSETS="$(ENVTEST_ASSETS)" go test -race -count=1 ./...

##@ Images / e2e

IMG_CONTROLLER ?= snapshot-controller:dev
IMG_WORKER     ?= snapshot-worker:dev

docker-build:
	docker build -t $(IMG_CONTROLLER) -f Dockerfile .
	docker build -t $(IMG_WORKER) -f worker/Dockerfile worker/

kind-load: docker-build
	$(KIND) load docker-image $(IMG_CONTROLLER) --name $(KIND_CLUSTER)
	$(KIND) load docker-image $(IMG_WORKER) --name $(KIND_CLUSTER)

e2e: tools generate docker-build
	ENVTEST_ASSETS="" bash hack/e2e.sh

e2e-down:
	-$(KIND) delete cluster --name $(KIND_CLUSTER)

clean:
	rm -rf $(BIN_DIR)
