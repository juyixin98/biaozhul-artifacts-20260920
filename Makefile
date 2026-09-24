.PHONY: help build test test-contracts test-api deploy server demo clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

build: ## Compile Solidity contracts
	forge build

test: test-contracts test-api ## Run all tests (requires local Anvil toolchain)

test-contracts: ## Run Foundry contract tests
	forge test -vv

deploy: ## Deploy contracts to the local Anvil node (RPC_URL / 127.0.0.1:8545)
	. .venv/bin/activate && python -m service.deploy

server: ## Start the FastAPI server on 127.0.0.1:8000
	. .venv/bin/activate && uvicorn service.app:create_app --factory --host 127.0.0.1 --port 8000

test-api: ## Run pytest API/integration tests (boots its own Anvil)
	. .venv/bin/activate && pytest

demo: ## End-to-end HTTP demo against a running Anvil + fresh deployment
	. .venv/bin/activate && python -m service.demo

clean: ## Remove build artifacts
	rm -rf out cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
