CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic

BUILD := build

.PHONY: all test run-test e2e bench check clean demo

all: $(BUILD)/topo_srv $(BUILD)/test_topo

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/topo_srv: src/main.cpp src/topo.hpp src/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc src/main.cpp -o $@

$(BUILD)/test_topo: tests/test_topo.cpp src/topo.hpp src/naive_topo.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc tests/test_topo.cpp -o $@

$(BUILD)/bench: tests/bench.cpp src/topo.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc tests/bench.cpp -o $@

$(BUILD)/compare: tests/compare.cpp src/topo.hpp src/naive_topo.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc tests/compare.cpp -o $@

# Full automated suite: C++ differential tests + Python end-to-end tests.
test: $(BUILD)/test_topo $(BUILD)/topo_srv
	./$(BUILD)/test_topo
	python3 tests/e2e_server.py

run-test: test

e2e: $(BUILD)/topo_srv
	python3 tests/e2e_server.py

bench: $(BUILD)/bench
	./$(BUILD)/bench

compare: $(BUILD)/compare
	./$(BUILD)/compare

check: test

demo: $(BUILD)/topo_srv
	./$(BUILD)/topo_srv < examples/request.jsonl

clean:
	rm -rf $(BUILD)
