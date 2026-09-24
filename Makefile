# CRD migration compatibility demo - Makefile
#
# Common targets:
#   make build             compile webhook + migrator binaries
#   make test              fast unit tests (no cluster)
#   make integration-test  envtest end-to-end tests (downloads k8s binaries)
#   make fixtures          (re)generate on-disk migration record fixtures
#   make verify            tidy + vet + unit + integration
#
# KUBEBUILDER_ASSETS is resolved automatically via setup-envtest. If your
# environment cannot reach Google's GCS (setup-envtest's legacy bucket),
# point the variable at a pre-extracted envtest dir:
#   export KUBEBUILDER_ASSETS=$HOME/.local/share/envtest-binaries/controller-tools/envtest

# Pin the control plane to the same minor as k8s.io/* dependencies.
ENVTEST_K8S_VERSION ?= 1.31.0
GO ?= go

# setup-envtest: prefer an installed binary; otherwise resolve through
# `go run` with a release that builds on Go 1.22.
SETUP_ENVTEST := $(shell command -v setup-envtest 2>/dev/null)

.PHONY: all
all: build

.PHONY: build
build:
	$(GO) build ./cmd/webhook ./cmd/migrator

.PHONY: test
test:
	$(GO) test ./api/... ./internal/... ./cmd/...

.PHONY: integration-test
integration-test:
	@KASSETS="$(KUBEBUILDER_ASSETS)"; \
	if [ -z "$$KASSETS" ]; then \
		if [ -n "$(SETUP_ENVTEST)" ]; then \
			KASSETS="$$($(SETUP_ENVTEST) use -i -p path $(ENVTEST_K8S_VERSION) 2>/dev/null || true)"; \
		fi; \
	fi; \
	if [ -z "$$KASSETS" ] && [ -x "$$HOME/.local/share/envtest-binaries/controller-tools/envtest/kube-apiserver" ]; then \
		KASSETS="$$HOME/.local/share/envtest-binaries/controller-tools/envtest"; \
	fi; \
	if [ -z "$$KASSETS" ]; then \
		echo "ERROR: KUBEBUILDER_ASSETS not set and no envtest $(ENVTEST_K8S_VERSION) installed."; \
		echo "Install binaries with: make envtest-download  (or export KUBEBUILDER_ASSETS)"; \
		exit 1; \
	fi; \
	echo "using envtest assets: $$KASSETS"; \
	KUBEBUILDER_ASSETS="$$KASSETS" $(GO) test -tags integration -count=1 -v ./test/integration/

# Manual fallback when the legacy setup-envtest GCS bucket is unreachable:
# pulls the same binaries from the GitHub release index.
.PHONY: envtest-download
envtest-download:
	scripts/fetch-envtest.sh $(ENVTEST_K8S_VERSION)

.PHONY: fixtures
fixtures:
	@KASSETS="$(KUBEBUILDER_ASSETS)"; \
	if [ -z "$$KASSETS" ] && [ -n "$(SETUP_ENVTEST)" ]; then \
		KASSETS="$$($(SETUP_ENVTEST) use -i -p path $(ENVTEST_K8S_VERSION) 2>/dev/null || true)"; \
	fi; \
	if [ -z "$$KASSETS" ]; then KASSETS="$(HOME)/.local/share/envtest-binaries/controller-tools/envtest"; fi; \
	KUBEBUILDER_ASSETS="$$KASSETS" FIXTURE_OUT_DIR="$(CURDIR)/fixtures/migration" \
		$(GO) test -tags integration -count=1 -v -run 'TestStorageMigrationAndFailureRecord|TestFixtureExport_SuccessfulUpRecord' ./test/integration/

.PHONY: vet
vet:
	$(GO) vet ./...
	$(GO) vet -tags integration ./test/...

.PHONY: fmt
fmt:
	$(GO) fmt ./...

.PHONY: tidy
tidy:
	$(GO) mod tidy

.PHONY: verify
verify: tidy vet test integration-test

.PHONY: clean
clean:
	rm -f webhook storage-migrator
