# 多边形裁剪子集 — 纯后端
# 仅依赖标准 C++17，无第三方库。

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic
BUILD := build
SRC := src

.PHONY: all test clean run-tests

all: $(BUILD)/polygon_clip

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/polygon_clip: $(SRC)/main.cpp $(SRC)/geometry.hpp $(SRC)/json.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -I $(SRC) $(SRC)/main.cpp -o $@

$(BUILD)/test_clip: $(SRC)/test_clip.cpp $(SRC)/geometry.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -I $(SRC) $(SRC)/test_clip.cpp -o $@

test: $(BUILD)/test_clip $(BUILD)/polygon_clip
	$(BUILD)/test_clip
	python3 tests/test_integration.py

run-tests: test

clean:
	rm -rf $(BUILD)
