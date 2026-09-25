CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic
BUILD    := build

.PHONY: all test clean

all: $(BUILD)/simplify $(BUILD)/run_tests

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/simplify: src/main.cpp src/service.hpp src/douglas_peucker.hpp src/geometry.hpp src/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/main.cpp

$(BUILD)/run_tests: tests/test_main.cpp src/service.hpp src/douglas_peucker.hpp src/geometry.hpp src/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ tests/test_main.cpp

test: $(BUILD)/run_tests
	./$(BUILD)/run_tests

clean:
	rm -rf $(BUILD)
