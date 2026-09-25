CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow
SANITIZE_FLAGS ?= -fsanitize=address,undefined -fno-omit-frame-pointer

SRC_DIR := src
BUILD_DIR := build

LIB_SRCS := $(SRC_DIR)/json.cpp $(SRC_DIR)/graph.cpp $(SRC_DIR)/dom.cpp \
            $(SRC_DIR)/naive.cpp $(SRC_DIR)/api.cpp
LIB_OBJS := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD_DIR)/%.o,$(LIB_SRCS))

BIN := $(BUILD_DIR)/domtree_backend
TEST_BIN := $(BUILD_DIR)/unit_tests

.PHONY: all test clean run-test

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) -c $< -o $@

$(BIN): $(LIB_OBJS) $(SRC_DIR)/main.cpp
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) $(LIB_OBJS) $(SRC_DIR)/main.cpp -o $@

# Unit tests are built with sanitizers for memory/UB evidence.
$(TEST_BIN): $(LIB_SRCS) tests/unit_tests.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(SANITIZE_FLAGS) -I$(SRC_DIR) \
	  $(LIB_SRCS) tests/unit_tests.cpp -o $@

test: $(TEST_BIN) $(BIN)
	$(TEST_BIN)
	python3 tests/cli_tests.py --bin $(BIN)

clean:
	rm -rf $(BUILD_DIR)
