CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic
BUILD := build

BINARIES := $(BUILD)/scc_backend $(BUILD)/scc_verify

all: $(BINARIES)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/json.o: src/json.cpp src/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c -o $@ $<

$(BUILD)/scc.o: src/scc.cpp src/scc.hpp src/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c -o $@ $<

$(BUILD)/scc_backend: src/main.cpp $(BUILD)/json.o $(BUILD)/scc.o | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/main.cpp $(BUILD)/json.o $(BUILD)/scc.o

$(BUILD)/scc_verify: src/verify_main.cpp $(BUILD)/json.o $(BUILD)/scc.o | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/verify_main.cpp $(BUILD)/json.o $(BUILD)/scc.o

test: all
	python3 tests/run_tests.py

clean:
	rm -rf $(BUILD)

.PHONY: all test clean
