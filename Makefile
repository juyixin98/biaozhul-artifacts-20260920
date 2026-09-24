.PHONY: help install build test forge-test anvil deploy api demo clean

help:
	@echo "可用目标："
	@echo "  make install     # 创建 .venv 并安装锁定的 Python 依赖"
	@echo "  make build       # forge 编译合约（输出 out/）"
	@echo "  make forge-test  # Foundry 单元/模糊/不变量测试（无需 anvil）"
	@echo "  make anvil       # 启动本机 anvil (127.0.0.1:8545)"
	@echo "  make deploy      # 部署 MockERC20 + ShareVault 到本机 anvil"
	@echo "  make api         # 启动 FastAPI 服务 (127.0.0.1:8000)"
	@echo "  make demo        # 运行 web3.py 端到端示例（需先 anvil+deploy）"
	@echo "  make test        # Python pytest 集成测试（自动管理测试 anvil:8555）"
	@echo "  make clean       # 删除构建产物与虚拟环境"

install:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -r requirements-lock.txt

build:
	forge build

forge-test:
	forge test -vv

test:
	. .venv/bin/activate && python -m pytest

anvil:
	anvil --port 8545 --gas-limit 30000000

deploy:
	. .venv/bin/activate && python scripts/deploy.py

api:
	. .venv/bin/activate && uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

demo:
	. .venv/bin/activate && python scripts/demo.py

clean:
	rm -rf out cache broadcast .venv .pytest_cache deployments
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
