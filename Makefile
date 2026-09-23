# Makefile - 曲线简化误差界后端
# 仅依赖标准 C++17，无第三方库。

CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
LDFLAGS  ?=

BUILD := build
SRC   := src
BIN   := $(BUILD)/simplify
TEST_BIN := $(BUILD)/test_unit

LIB_SRCS := $(SRC)/geometry.cpp $(SRC)/simplification.cpp $(SRC)/json.cpp
LIB_OBJS := $(patsubst $(SRC)/%.cpp,$(BUILD)/%.o,$(LIB_SRCS))

HEADERS := $(wildcard $(SRC)/*.hpp)

.PHONY: all unit-test test clean

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: $(SRC)/%.cpp $(HEADERS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): $(SRC)/main.cpp $(LIB_OBJS) $(HEADERS)
	$(CXX) $(CXXFLAGS) $< $(LIB_OBJS) -o $@ $(LDFLAGS)

$(TEST_BIN): tests/test_unit.cpp $(LIB_OBJS) $(HEADERS)
	$(CXX) $(CXXFLAGS) $< $(LIB_OBJS) -o $@ $(LDFLAGS)

unit-test: $(TEST_BIN)
	./$(TEST_BIN)

# 全量自动化测试：C++ 单元测试 + Python 端到端测试
test: $(BIN) $(TEST_BIN)
	./$(TEST_BIN)
	python3 tests/run_e2e.py --bin ./$(BIN)

clean:
	rm -rf $(BUILD)
