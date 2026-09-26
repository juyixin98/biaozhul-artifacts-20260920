CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion

SRC_DIR := src
BUILD_DIR := build
BIN := $(BUILD_DIR)/mincut
TEST_BIN := $(BUILD_DIR)/unit_tests

LIB_SRCS := $(SRC_DIR)/json.cpp $(SRC_DIR)/brute.cpp $(SRC_DIR)/verifier.cpp
LIB_OBJS := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD_DIR)/%.o,$(LIB_SRCS))

.PHONY: all test check clean

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): $(LIB_OBJS) $(BUILD_DIR)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD_DIR)/main.o: $(SRC_DIR)/main.cpp $(SRC_DIR)/*.h | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(TEST_BIN): $(LIB_OBJS) $(BUILD_DIR)/unit_tests.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD_DIR)/unit_tests.o: $(SRC_DIR)/unit_tests.cpp $(SRC_DIR)/*.h | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

test: $(TEST_BIN)
	$(TEST_BIN)

# Full automated suite: C++ unit tests + Python integration tests.
check: $(BIN) $(TEST_BIN)
	$(TEST_BIN)
	python3 tests/run_tests.py

clean:
	rm -rf $(BUILD_DIR)
