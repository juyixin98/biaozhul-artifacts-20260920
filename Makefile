# 多边形裁剪子集 - 纯后端
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Iinclude
LDFLAGS  ?=

SRC      := src/geometry.cpp src/json.cpp
BIN      := build/polygon-clip
TEST_BIN := build/unit_tests

.PHONY: all test check clean run-demo

all: $(BIN)

$(BIN): $(SRC) src/main.cpp | build
	$(CXX) $(CXXFLAGS) $(SRC) src/main.cpp -o $@ $(LDFLAGS)

$(TEST_BIN): $(SRC) tests/unit_tests.cpp | build
	$(CXX) $(CXXFLAGS) $(SRC) tests/unit_tests.cpp -o $@ $(LDFLAGS)

build:
	mkdir -p build

# C++ 单元测试 + Python 端到端/模糊测试
test check: $(BIN) $(TEST_BIN)
	$(TEST_BIN)
	python3 tests/test_e2e.py
	python3 tests/test_fuzz.py

run-demo: $(BIN)
	./scripts/run_demo.sh

clean:
	rm -rf build
