# Event Rollback Indexer — convenience targets.
# Requires: Foundry (~/.foundry/bin on PATH), Python venv at .venv.

SHELL := /bin/bash
export PATH := $(HOME)/.foundry/bin:$(PATH)

.PHONY: help install build test test-contracts test-py demo anvil deploy run

help:
	@echo "make install   create .venv and install locked Python deps"
	@echo "make build     forge build (compiles contracts/out artifact)"
	@echo "make test      forge test + full pytest (launches temp Anvils)"
	@echo "make demo      acyclic local-Anvil live reorg demo"
	@echo "make anvil     start a local Anvil on 127.0.0.1:8545"

install:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -r requirements.lock

build:
	forge build

test-contracts:
	forge test

test-py:
	. .venv/bin/activate && python -m pytest

test: test-contracts test-py

demo:
	. .venv/bin/activate && python scripts/live_demo.py

anvil:
	anvil --host 127.0.0.1 --port 8545
