CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
LDFLAGS ?=

SRC_DIR := src
BUILD_DIR := build

LIB_SOURCES := $(SRC_DIR)/json.cpp $(SRC_DIR)/decimal.cpp $(SRC_DIR)/geometry.cpp $(SRC_DIR)/locator.cpp
LIB_OBJECTS := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD_DIR)/%.o,$(LIB_SOURCES))

BIN := pointloc
TEST_BIN := unit_tests

.PHONY: all clean test

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): $(LIB_OBJECTS) $(BUILD_DIR)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

$(TEST_BIN): $(LIB_OBJECTS) $(BUILD_DIR)/unit_tests.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

test: $(BIN) $(TEST_BIN)
	./$(TEST_BIN)
	python3 tests/differential_test.py ./$(BIN)
	python3 tests/e2e_test.py ./$(BIN)

clean:
	rm -rf $(BUILD_DIR) $(BIN) $(TEST_BIN)
