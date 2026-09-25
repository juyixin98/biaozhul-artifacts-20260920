# 精确线段相交 —— 纯后端
# 依赖：g++ (支持 C++17 与 __int128)、Boost.Multiprecision 头文件 (libboost-dev)
# 测试另需 python3（仅用于与有理数参考实现做随机对拍）。

CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
BOOST_INC ?= /usr/include

SRC_DIR  := src
BUILD    := build
BIN      := seginter

LIB_SRCS := $(SRC_DIR)/geometry.cpp $(SRC_DIR)/json.cpp
LIB_OBJS := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD)/%.o,$(LIB_SRCS))

.PHONY: all test check clean random-test samples

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: $(SRC_DIR)/%.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) -c $< -o $@

$(BIN): $(BUILD)/main.o $(LIB_OBJS)
	$(CXX) $(CXXFLAGS) $^ -o $@

# 单元测试（含零长线段、大坐标、交换端点不变性）
$(BUILD)/unit_tests: tests/unit_tests.cpp $(LIB_OBJS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) $< $(LIB_OBJS) -o $@

test: $(BIN) $(BUILD)/unit_tests
	$(BUILD)/unit_tests

# 随机小坐标与 Python 有理数参考实现对拍
random-test: $(BIN)
	python3 tests/random_compare.py --binary ./$(BIN) --count 20000 --coord-range 8 --seed 20260923

# 大坐标随机对拍（接近坐标上界，验证不溢出）
random-test-large: $(BIN)
	python3 tests/random_compare.py --binary ./$(BIN) --count 5000 --coord-range 4611686018427387902 --seed 987654321

check: test random-test random-test-large
	@echo "ALL CHECKS PASSED"

# 用样例请求实际跑一遍并保存输出
samples: $(BIN)
	@for f in samples/request_*.json; do \
	  out="samples/response_$${f##*request_}"; \
	  ./$(BIN) --pretty "$$f" -o "$$out" && echo "wrote $$out"; \
	done

clean:
	rm -rf $(BUILD) $(BIN)
