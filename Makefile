SHELL := /bin/bash
export PATH := $(HOME)/.foundry/bin:$(PATH)

.PHONY: help install build test-forge test-py test deploy run clean

help: ## 显示可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## 安装 Python 依赖（需先创建 .venv）
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

build: ## 编译 Solidity 合约
	forge build

test-forge: ## Solidity 合约单元/模糊/gas 测试
	forge test -vv

test-py: build ## Python 集成测试（自动启动 Anvil）
	.venv/bin/pytest

test: test-forge test-py ## 运行全部测试

anvil: ## 启动本地 Anvil（端口 8545）
	anvil --host 127.0.0.1 --port 8545

deploy: build ## 部署到本地链（需先 make anvil）
	.venv/bin/python scripts/deploy.py

run: ## 启动 HTTP API（需先设置 CONTRACT_ADDRESS）
	uvicorn app.main:app --host 127.0.0.1 --port 8000

clean:
	rm -rf out cache target
