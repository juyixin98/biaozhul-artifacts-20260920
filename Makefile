# Constant-product pool — developer commands.
# Everything is local; the only network call is optional dependency verification.

SHELL := /bin/bash
export PATH := $(HOME)/.foundry/bin:$(PATH)

.PHONY: help build test vectors replay verify-deps anvil fmt clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

build: ## Compile all contracts
	forge build

test: ## Run unit, security, edge, invariant and reference-vector tests
	forge test -vv

vectors: ## Regenerate reference vectors from the Python model
	python3 reference/generate_vectors.py

replay: ## End-to-end replay on a fresh ephemeral Anvil chain
	bash script/replay.sh

verify-deps: ## Byte-compare vendored forge-std against the pinned commit
	bash script/verify_deps.sh

anvil: ## Start a local Anvil node
	anvil --chain-id 31337

fmt: ## Format Solidity sources
	forge fmt

clean: ## Remove build artifacts
	forge clean
