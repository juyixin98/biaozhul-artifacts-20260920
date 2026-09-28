.PHONY: install test test-unit test-integration cov run fixtures clean

install:
	pip install -e .

test:
	bash scripts/run_tests.sh

test-unit:
	bash scripts/run_tests.sh tests/unit -m "not slow"

test-integration:
	bash scripts/run_tests.sh tests/integration

cov:
	python3 -m pytest --cov=src/ssp --cov-report=term-missing

run:
	bash scripts/run.sh

fixtures:
	PYTHONPATH=src python3 -m ssp.cli fixtures --trials 8000

clean:
	rm -rf .pytest_cache logs data/*.db src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
