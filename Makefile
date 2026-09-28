.PHONY: help install test cov run reproduce clean demo

PYTHON ?= python3
export PYTHONPATH=src

help:
	@echo "install   - install locked dependencies"
	@echo "test      - run the full pytest suite"
	@echo "cov       - run tests with coverage (>=80% gate)"
	@echo "run       - start the FastAPI service on 127.0.0.1:8000"
	@echo "demo      - run CLI reasoning over all fixtures (no server)"
	@echo "reproduce - full offline reproduction: tests + CLI + captured results"
	@echo "clean     - remove local db / caches / generated results"

install:
	$(PYTHON) -m pip install -r requirements.lock

test:
	$(PYTHON) -m pytest -q

cov:
	$(PYTHON) -m pytest --cov=src/min_iowl --cov-report=term-missing --cov-fail-under=80 -q

run:
	uvicorn min_iowl.api.app:app --host 127.0.0.1 --port 8000

demo:
	@for f in data/fixture_hierarchy.json data/fixture_intersection_disjoint.json data/fixture_mutex_instance.json; do \
	  echo; $(PYTHON) scripts/reason.py $$f; done
	@echo; $(PYTHON) scripts/reason.py --functional data/fixture_hierarchy.owlf

reproduce:
	bash scripts/reproduce.sh

clean:
	rm -rf .pytest_cache __pycache__ src/**/__pycache__ tests/__pycache__
	rm -f data/miniowl.sqlite3 data/miniowl.sqlite3-wal data/miniowl.sqlite3-shm
	rm -rf results/
