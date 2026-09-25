# SPDX-License-Identifier: MIT
# 精确线段相交后端 —— 零外部依赖，仅需支持 C++20 的编译器。
CXX      ?= g++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -Wpedantic -Iinclude
BUILD    := build
SRC      := src/bigint.cpp src/json.cpp src/geometry.cpp

CLI      := $(BUILD)/segi-cli
TESTBIN  := $(BUILD)/segi-tests

.PHONY: all test check clean

all: $(CLI) $(TESTBIN)

$(CLI): $(SRC) src/main.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRC) src/main.cpp -o $@

$(TESTBIN): $(SRC) tests/test_segments.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRC) tests/test_segments.cpp -o $@

$(BUILD):
	mkdir -p $(BUILD)

# C++ 内嵌单元/性质测试
test: $(TESTBIN)
	./$(TESTBIN)

# 完整验收：C++ 测试 + 与 Python 有理参考实现的随机对拍
check: $(CLI) $(TESTBIN)
	./$(TESTBIN)
	python3 tests/crosscheck.py --cli ./$(CLI) --seed 20260923 --count 4000

clean:
	rm -rf $(BUILD)
