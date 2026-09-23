# mirror-admission — offline local image admission backend

GO ?= go
BIN_DIR := bin
SERVER_BIN := $(BIN_DIR)/mirror-admission-server
VERIFIER_BIN := $(BIN_DIR)/verifier
GEN_BIN := $(BIN_DIR)/genexamples
LOCK_BIN := $(BIN_DIR)/policylock

# Default trust material / allowlist locations (overridable via env).
KEYS_DIR := keys
EX_DIR := examples/generated
REPORTS := data/reports.jsonl

.PHONY: all build test test-race vet fmt policy-lock examples keys run acceptance clean vendor tidy

all: build

build: $(SERVER_BIN) $(VERIFIER_BIN) $(GEN_BIN) $(LOCK_BIN)

$(BIN_DIR):
	mkdir -p $(BIN_DIR)

$(SERVER_BIN): $(BIN_DIR)
	$(GO) build -mod=vendor -o $@ ./cmd/server
$(VERIFIER_BIN): $(BIN_DIR)
	$(GO) build -mod=vendor -o $@ ./cmd/verifier
$(GEN_BIN): $(BIN_DIR)
	$(GO) build -mod=vendor -o $@ ./cmd/genexamples
$(LOCK_BIN): $(BIN_DIR)
	$(GO) build -mod=vendor -o $@ ./cmd/policylock

# Re-freeze the policy hash. DELIBERATE release action after policy review.
policy-lock: $(LOCK_BIN)
	$(LOCK_BIN)

# Generate two real Ed25519 keypairs for LOCAL use.
keys: $(VERIFIER_BIN)
	mkdir -p $(KEYS_DIR)
	$(VERIFIER_BIN) keygen -outdir $(KEYS_DIR)

# Regenerate the fully-signed example bundle (keys are regenerated each run).
examples: $(GEN_BIN) policy-lock
	rm -rf $(EX_DIR)
	$(GEN_BIN) -out $(EX_DIR)

# Vendor dependencies for hermetic/offline builds.
vendor:
	$(GO) mod tidy
	$(GO) mod vendor
	$(GO) mod verify

tidy:
	$(GO) mod tidy

test:
	$(GO) test -mod=vendor -count=1 ./...

test-race:
	$(GO) test -mod=vendor -race -count=1 ./...

vet:
	$(GO) vet -mod=vendor ./...

fmt:
	$(GO) fmt ./...

# Run the server against the generated examples.
run: build
	MIRRORAD_VERIFIER_PUB=$(EX_DIR)/keys/tester_public.pem \
	MIRRORAD_EXEMPT_PUB=$(EX_DIR)/keys/exempt_authority_public.pem \
	MIRRORAD_ALLOWLIST=$(EX_DIR)/allowlist.json \
	MIRRORAD_REPORTS=$(REPORTS) \
	MIRRORAD_LISTEN=:8080 \
	$(SERVER_BIN)

# One-command acceptance: builds offline, runs all tests, boots the server,
# posts every signed scenario, asserts decisions incl. boundary time.
acceptance: build
	./scripts/acceptance.sh

clean:
	rm -rf $(BIN_DIR) data
