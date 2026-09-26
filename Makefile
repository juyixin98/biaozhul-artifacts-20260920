CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow
SANITIZE ?=

SRC_DIR  := src
TEST_DIR := tests
BUILD    := build

LIB_SRCS := $(SRC_DIR)/json.cpp $(SRC_DIR)/topo.cpp
LIB_OBJS := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD)/%.o,$(LIB_SRCS))

BIN      := topo_server
TEST_BIN := test_topo

.PHONY: all test clean run-test

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: $(SRC_DIR)/%.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SANITIZE) -I$(SRC_DIR) -c $< -o $@

$(BIN): $(LIB_OBJS) $(SRC_DIR)/main.cpp
	$(CXX) $(CXXFLAGS) $(SANITIZE) -I$(SRC_DIR) $(SRC_DIR)/main.cpp $(LIB_OBJS) -o $@

$(TEST_BIN): $(LIB_SRCS) $(TEST_DIR)/test_topo.cpp
	$(CXX) $(CXXFLAGS) $(SANITIZE) -I$(SRC_DIR) $(TEST_DIR)/test_topo.cpp $(LIB_SRCS) -o $(TEST_BIN)

test: $(TEST_BIN)
	./$(TEST_BIN)

run-test: test

clean:
	rm -rf $(BUILD) $(BIN) $(TEST_BIN)
