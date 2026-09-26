CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
BUILD := build

all: $(BUILD)/dynconn $(BUILD)/test_random

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/dynconn: src/main.cpp src/json.hpp src/solver.hpp src/rollback_dsu.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc -o $@ src/main.cpp

$(BUILD)/test_random: tests/test_random.cpp src/solver.hpp src/rollback_dsu.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc -o $@ tests/test_random.cpp

test: all
	./tests/run_tests.sh

clean:
	rm -rf $(BUILD)

.PHONY: all test clean
