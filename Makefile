CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow
LDFLAGS  ?=

SRC_DIR  := src
BUILD    := build
BIN      := mincut-backend

CORE_SRCS := json.cpp maxflow.cpp mincut.cpp verifier.cpp bruteforce.cpp protocol.cpp
CORE_OBJS := $(addprefix $(BUILD)/,$(CORE_SRCS:.cpp=.o))

.PHONY: all clean test

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: $(SRC_DIR)/%.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): $(CORE_OBJS) $(BUILD)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

# Unit tests are a separate binary with its own main.
TEST_BIN := $(BUILD)/unit_tests
TEST_SRCS := tests/unit_tests.cpp
$(TEST_BIN): $(CORE_OBJS) $(TEST_SRCS)
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) $(CORE_OBJS) $(TEST_SRCS) -o $@ $(LDFLAGS)

test: $(BIN) $(TEST_BIN)
	python3 tests/run_tests.py

clean:
	rm -rf $(BUILD) $(BIN)
