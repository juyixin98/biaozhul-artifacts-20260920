# 球面距离查询 —— 纯后端，C++17，无第三方依赖。
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Iinclude -Isrc
BUILD    := build
BIN      := $(BUILD)/sphere_dist
TESTBIN  := $(BUILD)/test_geo

LIB_SRC  := src/geo.cpp src/json.cpp
LIB_OBJ  := $(BUILD)/geo.o $(BUILD)/json.o

.PHONY: all test clean check

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: src/%.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): src/main.cpp $(LIB_OBJ) | $(BUILD)
	$(CXX) $(CXXFLAGS) src/main.cpp $(LIB_OBJ) -o $@

$(TESTBIN): tests/unit/test_geo.cpp $(LIB_OBJ) | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/unit/test_geo.cpp $(LIB_OBJ) -o $@

# 单元测试 + 端到端集成测试
test: $(TESTBIN) $(BIN)
	$(TESTBIN)
	tests/integration/run_tests.sh "$(abspath $(BIN))"

clean:
	rm -rf $(BUILD)
