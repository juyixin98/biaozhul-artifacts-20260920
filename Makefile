# Joint Trajectory Time Parameterization — convenience targets.
BUILD ?= build
SERVER ?= $(BUILD)/jtp_server
PORT ?= 8080

.PHONY: all build test unit e2e verify-deps run clean fmt-check

all: build

build:
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Release
	cmake --build $(BUILD) -j

test: unit e2e

unit: build
	ctest --test-dir $(BUILD) --output-on-failure -R unit_tests

e2e: build
	bash scripts/e2e_test.sh "$$(pwd)/$(SERVER)" "$$(pwd)/examples"

verify-deps:
	bash scripts/verify_dependencies.sh

run: build
	$(SERVER) --host 127.0.0.1 --port $(PORT)

clean:
	rm -rf $(BUILD)
