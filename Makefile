.PHONY: help install demo unit integration test cov cluster clean

help:
	@echo "make demo         # 确定性场景演示（单进程）"
	@echo "make unit         # 快速测试（不派生子进程）"
	@echo "make integration  # 仅真实多进程端到端测试"
	@echo "make test         # 全部测试"
	@echo "make cov          # 全部测试 + 覆盖率"
	@echo "make cluster      # 真实多进程 HTTP 演示"

install:
	pip install -e ".[test]"

demo:
	python -m gradbucket.cli demo-all

unit:
	python -m pytest tests/ -q -m "not integration"

integration:
	python -m pytest tests/ -m integration -q

test:
	python -m pytest tests/ -q

cov:
	python -m pytest tests/ -q --cov=gradbucket --cov-report=term-missing

cluster:
	python -m gradbucket.cli cluster --workers 3 --samples 12

clean:
	rm -rf .pytest_cache .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
