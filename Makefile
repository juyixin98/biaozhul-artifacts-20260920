# 扫描线矩形并面积/周长 —— 纯后端
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
BUILD    := build
BIN      := $(BUILD)/rectunion
TEST_BIN := $(BUILD)/test_units

.PHONY: all test clean

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BIN): src/main.cpp src/geometry.hpp src/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc -o $@ src/main.cpp

$(TEST_BIN): tests/test_units.cpp tests/brute_force.hpp src/geometry.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -I. -o $@ tests/test_units.cpp

test: $(BIN) $(TEST_BIN)
	@$(TEST_BIN)
	@bash tests/integration_test.sh

clean:
	rm -rf $(BUILD)
