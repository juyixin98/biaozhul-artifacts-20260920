# 空间最近邻索引 — 构建脚本
# 目标：
#   make            构建主程序 build/spatial_index 与测试 build/test_spatial
#   make run        运行自动化测试
#   make e2e        运行端到端（CLI/JSON）测试
#   make check      全部测试（含 ASan/UBSan 构建）
#   make asan       仅在 AddressSanitizer + UBSan 下构建并运行单元测试
#   make clean
CXX      ?= g++
CXXSTD   := -std=c++17
WARN     := -Wall -Wextra -Wpedantic
OPT      := -O2
CXXFLAGS ?= $(CXXSTD) $(WARN) $(OPT)
LDFLAGS  ?=

BUILD := build
SRC   := src
TST   := tests

BIN  := $(BUILD)/spatial_index
TBIN := $(BUILD)/test_spatial

.PHONY: all run e2e check asan clean

all: $(BIN) $(TBIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BIN): $(SRC)/main.cpp $(SRC)/spatial.cpp $(SRC)/spatial.hpp $(SRC)/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRC)/main.cpp $(SRC)/spatial.cpp -o $@ $(LDFLAGS)

$(TBIN): $(TST)/test_spatial.cpp $(SRC)/spatial.cpp $(SRC)/spatial.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -I$(SRC) $(TST)/test_spatial.cpp $(SRC)/spatial.cpp -o $@ $(LDFLAGS)

run: $(TBIN)
	./$(TBIN)

e2e: $(BIN)
	./tests/run_e2e.sh

asan: | $(BUILD)
	$(CXX) $(CXXSTD) $(WARN) -O1 -g -Isrc -fsanitize=address,undefined -fno-omit-frame-pointer \
	  $(TST)/test_spatial.cpp $(SRC)/spatial.cpp -o $(BUILD)/test_spatial_asan
	./$(BUILD)/test_spatial_asan

check: run e2e asan

clean:
	rm -rf $(BUILD)
