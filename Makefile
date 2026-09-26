# SPDX-License-Identifier: MIT
CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
LDFLAGS ?=

SRC_DIR := src
BUILD_DIR := build
BIN := dagpaths

LIB_SRCS := $(SRC_DIR)/bigint.h $(SRC_DIR)/graph.h $(SRC_DIR)/json.h $(SRC_DIR)/api.h \
            $(SRC_DIR)/graph.cpp $(SRC_DIR)/json.cpp $(SRC_DIR)/api.cpp

.PHONY: all clean test unit-test integration-test

all: $(BIN)

$(BIN): $(SRC_DIR)/main.cpp $(LIB_SRCS)
	$(CXX) $(CXXFLAGS) $(SRC_DIR)/main.cpp $(SRC_DIR)/graph.cpp $(SRC_DIR)/json.cpp \
		$(SRC_DIR)/api.cpp -o $@ $(LDFLAGS)

$(BUILD_DIR)/unit_tests: tests/unit_tests.cpp $(LIB_SRCS)
	@mkdir -p $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) tests/unit_tests.cpp $(SRC_DIR)/graph.cpp $(SRC_DIR)/json.cpp \
		$(SRC_DIR)/api.cpp -o $@ $(LDFLAGS)

unit-test: $(BUILD_DIR)/unit_tests
	$(BUILD_DIR)/unit_tests

integration-test: $(BIN)
	python3 tests/test_integration.py --binary ./$(BIN)

test: unit-test integration-test

clean:
	rm -rf $(BUILD_DIR) $(BIN)
