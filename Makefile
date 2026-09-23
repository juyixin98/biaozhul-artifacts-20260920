.PHONY: help build test test-gas fmt demo clean anvil-a anvil-b deploy-a deploy-b

help: ## 显示可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## 编译合约
	forge build

test: ## 运行全部单元/场景测试
	forge test -vv

test-gas: ## 运行测试并输出 gas 报告
	forge test --gas-report

fmt: ## 格式化 Solidity 代码
	forge fmt

demo: ## 启动双链 anvil 并执行端到端回放（成功兑换 + 负面用例 + 双方退款）
	bash script/demo.sh

anvil-a: ## 单独启动链 A（:8545）
	anvil --chain-id 31337 --port 8545

anvil-b: ## 单独启动链 B（:8546）
	anvil --chain-id 31338 --port 8546

PK := 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80

deploy-a: ## 部署到链 A（需链在运行）
	TOKEN_NAME="Token A" TOKEN_SYMBOL=TKA PRIVATE_KEY=$(PK) \
	forge script script/Deploy.s.sol:Deploy --rpc-url chain_a --broadcast

deploy-b: ## 部署到链 B（需链在运行）
	TOKEN_NAME="Token B" TOKEN_SYMBOL=TKB PRIVATE_KEY=$(PK) \
	forge script script/Deploy.s.sol:Deploy --rpc-url chain_b --broadcast

clean: ## 清理构建产物与本地运行目录
	forge clean
	rm -rf .run broadcast
