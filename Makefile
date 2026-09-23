# meshcheck — offline triangular mesh topology validator
CXX      ?= g++
CXXSTD   ?= -std=c++17
CXXFLAGS ?= -O2 -Wall -Wextra -Wpedantic $(CXXSTD)
LDFLAGS  ?=

BUILD := build
BIN   := $(BUILD)/meshcheck
SRC   := src/main.cpp src/mesh_check.cpp
HDR   := src/json.hpp src/mesh_check.hpp

.PHONY: all test clean

all: $(BIN)

$(BIN): $(SRC) $(HDR) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRC) -o $@ $(LDFLAGS)

$(BUILD):
	mkdir -p $(BUILD)

test: $(BIN)
	python3 tests/run_tests.py

clean:
	rm -rf $(BUILD) tests/out
