CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow

SRC := src/dominance.cpp
HDR := src/json.hpp src/graph.hpp src/dominance.hpp

BUILD := build

.PHONY: all test clean run-test

all: $(BUILD)/dom-server $(BUILD)/test_dom

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/dom-server: src/main.cpp $(SRC) $(HDR) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/main.cpp $(SRC)

$(BUILD)/test_dom: tests/test_dom.cpp $(SRC) $(HDR) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ tests/test_dom.cpp $(SRC)

test: $(BUILD)/test_dom
	./$(BUILD)/test_dom

clean:
	rm -rf $(BUILD)
