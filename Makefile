# 网格拓扑校验 —— 纯后端，无第三方依赖
CXX      ?= g++
CXXSTD   := -std=c++17
CXXFLAGS ?= -O2 -Wall -Wextra -Wpedantic
CPPFLAGS := -Iinclude

BUILD := build
SRCS  := src/json.cpp src/mesh.cpp src/topology.cpp
OBJS  := $(SRCS:src/%.cpp=$(BUILD)/%.o)

BIN   := $(BUILD)/gridtopo_check
TBIN  := $(BUILD)/gridtopo_tests

.PHONY: all test clean
all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: src/%.cpp | $(BUILD)
	$(CXX) $(CXXSTD) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

$(BIN): $(OBJS) $(BUILD)/main.o
	$(CXX) $(CXXSTD) $(CXXFLAGS) $^ -o $@

$(BUILD)/main.o: src/main.cpp | $(BUILD)
	$(CXX) $(CXXSTD) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

$(TBIN): $(OBJS) $(BUILD)/test_topology.o
	$(CXX) $(CXXSTD) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_topology.o: tests/test_topology.cpp | $(BUILD)
	$(CXX) $(CXXSTD) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

# C++ 单元测试 + 端到端样例断言
test: $(TBIN) $(BIN)
	$(TBIN)
	python3 tests/run_e2e.py

clean:
	rm -rf $(BUILD)
