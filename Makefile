# Recoverable Snapshot Controller — Makefile
#
# Common targets:
#   make build        compile manager + verifier
#   make test         unit tests (digest + shell/Go cross-impl equivalence)
#   make envtest      controller integration tests against a real apiserver
#                     (auto-downloads envtest binaries on first run)
#   make e2e          full kind end-to-end (scripts/e2e.sh)
#   make docker-build build the manager image
#   make kind-up      create the local kind cluster
#   make deploy       load image + apply CRDs/RBAC/Deployment

SHELL := /usr/bin/env bash
GO ?= go
REG ?= localhost:5000
CLUSTER ?= snapshot-p081a
CTX ?= kind-$(CLUSTER)
IMG ?= $(REG)/snapshot-controller:dev
WORKER_IMG ?= $(REG)/snapshot-worker:dev
KIND ?= kind
KUBECTL ?= kubectl
ENVTEST_K8S_VERSION ?= 1.30.x
SETUP_ENVTEST := $(shell go env GOPATH)/bin/setup-envtest
ENVTEST_BIN_DIR := $(shell go env GOPATH)/bin/envtest-bin

.PHONY: all build test vet fmt envtest e2e docker-build kind-up kind-down deploy undeploy samples clean tidy

all: build

build:
	$(GO) build -o bin/manager ./cmd/manager
	$(GO) build -o bin/verifier ./cmd/verifier

tidy:
	$(GO) mod tidy

fmt:
	$(GO) fmt ./...

vet:
	$(GO) vet ./...

test: vet
	$(GO) test ./internal/digest/... ./internal/worker/... -count=1

# Integration tests. setup-envtest can fail to list versions behind some
# proxies; the scripts know how to fall back to a direct GitHub download.
envtest:
	KUBEBUILDER_ASSETS="$$(scripts/envtest-assets.sh)" $(GO) test ./internal/controller/... -count=1

# Build and push both images to the local registry. A registry is used instead
# of `kind load` because the latter fails on hosts using Docker's containerd
# image store ("failed to detect containerd snapshotter").
docker-build:
	docker build -t $(IMG) .
	docker push $(IMG)
	mkdir -p buildctx
	cp "$$(command -v kubectl)" buildctx/kubectl
	docker build -f Dockerfile.worker -t $(WORKER_IMG) buildctx
	docker push $(WORKER_IMG)

kind-up:
	$(KIND) get clusters 2>/dev/null | grep -q '^$(CLUSTER)$$' || \
	  $(KIND) create cluster --config kind-cluster.yaml
	bash scripts/devregistry.sh $(CLUSTER)

kind-down:
	$(KIND) delete cluster --name $(CLUSTER) || true

deploy: kind-up docker-build
	$(KUBECTL) --context $(CTX) apply -f config/crd/bases/
	$(KUBECTL) --context $(CTX) apply -f config/manager/manager.yaml
	$(KUBECTL) --context $(CTX) -n snapshot-system rollout status deploy/snapshot-controller --timeout=120s
	$(KUBECTL) --context $(CTX) apply -f config/rbac/worker.yaml

undeploy:
	-$(KUBECTL) --context $(CTX) delete -f config/rbac/worker.yaml
	-$(KUBECTL) --context $(CTX) delete -f config/manager/manager.yaml
	-$(KUBECTL) --context $(CTX) delete -f config/crd/bases/

samples:
	$(KUBECTL) apply -f config/samples/

e2e:
	scripts/e2e.sh

clean:
	rm -rf bin
