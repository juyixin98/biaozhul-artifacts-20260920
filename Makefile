CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
LDFLAGS ?=

BUILD_DIR := build
BIN := $(BUILD_DIR)/bipartite-matching

SOURCES := src/main.cpp src/bipartite.cpp src/brute.cpp src/protocol.cpp src/http_server.cpp
OBJECTS := $(patsubst src/%.cpp,$(BUILD_DIR)/%.o,$(SOURCES))
HEADERS := src/json.hpp src/bipartite.hpp src/brute.hpp src/protocol.hpp src/http_server.hpp

.PHONY: all clean test

all: $(BIN)

$(BIN): $(OBJECTS) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $(OBJECTS) $(LDFLAGS)

$(BUILD_DIR)/%.o: src/%.cpp $(HEADERS) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c -o $@ $<

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

test: $(BIN)
	python3 tests/run_tests.py

clean:
	rm -rf $(BUILD_DIR)
