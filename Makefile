SHELL := /usr/bin/env bash

IMG ?= quota-reserver:latest
CLUSTER_NAME ?= quota-reserver-e2e
ENVTEST_K8S_VERSION ?= 1.31.0
CONTROLLER_GEN := go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.16.5
SETUP_ENVTEST := go run sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.19

.PHONY: all generate manifests fmt vet build test test-unit test-envtest \
        docker-build bundle deploy e2e acceptance clean

all: build

## Generate deepcopy methods.
generate:
	$(CONTROLLER_GEN) object paths="./api/..."

## Generate CRD manifests into config/crd.
manifests:
	$(CONTROLLER_GEN) crd paths="./api/..." output:crd:dir=config/crd

fmt:
	go fmt ./...

vet:
	go vet ./...

## Build the manager binary.
build: generate fmt vet
	go build -o bin/manager ./cmd/manager

## Fast unit tests (no cluster needed).
test-unit:
	go test ./pkg/... ./api/... ./internal/... -count=1

## Integration tests against a real apiserver (envtest; downloads etcd+kube-apiserver).
test-envtest: generate manifests
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use $(ENVTEST_K8S_VERSION) -p path)" \
		go test ./test/envtest/... -count=1 -v

## All Go tests.
test: test-unit test-envtest

## Build the container image.
docker-build:
	docker build -t $(IMG) .

## Regenerate the single-file installer config/install.yaml.
bundle: manifests
	./scripts/bundle.sh

## Deploy to the current kubectl context.
deploy: bundle
	kubectl apply -f config/install.yaml

## Full end-to-end: kind cluster + image + deploy + acceptance.
e2e: bundle
	CLUSTER_NAME=$(CLUSTER_NAME) ./scripts/e2e-kind.sh

## Acceptance suite against the current kubectl context.
acceptance:
	./scripts/acceptance.sh

clean:
	rm -rf bin/
