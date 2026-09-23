# 三维射线-包围盒求交后端 —— 纯标准库、无第三方依赖
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
CPPFLAGS += -Iinclude

BUILD := build
LIB_SRC := src/app/query.cpp src/geo/ray_box.cpp src/geo/bvh.cpp src/json/json.cpp
LIB_OBJ := $(patsubst src/%.cpp,$(BUILD)/%.o,$(LIB_SRC))

BIN := ray_box_backend
TESTBIN := test_ray_box

.PHONY: all test check clean

all: $(BIN)

$(BIN): $(LIB_OBJ) $(BUILD)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/%.o: src/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

# ---- 测试 ----
$(TESTBIN): $(LIB_OBJ) $(BUILD)/test_main.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_main.o: tests/test_main.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

# C++ 单元测试 + Python 端到端测试
test check: $(TESTBIN)
	./$(TESTBIN)
	python3 tests/e2e_test.py

clean:
	rm -rf $(BUILD) $(BIN) $(TESTBIN)
