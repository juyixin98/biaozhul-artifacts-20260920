.PHONY: install test coverage validate run clean

PY := .venv/bin/python

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[test]"

test:
	PYTHONPATH=src $(PY) -m pytest -q

coverage:
	PYTHONPATH=src $(PY) -m pytest -q --cov=sample_size_planner --cov-report=term-missing

validate:
	PYTHONPATH=src $(PY) scripts/run_validation.py --replications 40000

run:
	PYTHONPATH=src $(PY) -m uvicorn sample_size_planner.api.main:app --host 127.0.0.1 --port 8000

clean:
	rm -rf .pytest_cache htmlcov .coverage build dist *.egg-info
	find . -path ./.venv -prune -o -name __pycache__ -type d -print -exec rm -rf {} +
