.PHONY: build layouts test test-unit test-int demo serve clean

PATH := $(HOME)/.foundry/bin:$(PATH)

build:
	forge build

layouts:
	. .venv/bin/activate && python script/export_layouts.py

test:
	. .venv/bin/activate && python -m pytest -q

test-unit:
	. .venv/bin/activate && python -m pytest tests/unit tests/api/test_api.py -q -k "not flow"

demo:
	. .venv/bin/activate && python script/demo_upgrade.py

serve:
	. .venv/bin/activate && uvicorn api.main:app --reload --port 8000

clean:
	rm -rf out cache .pytest_cache
