# Makefile — 球面距离查询后端
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
SRCDIR   := src
BUILDDIR := build

LIB_SRCS := $(SRCDIR)/geo.cpp $(SRCDIR)/json.cpp $(SRCDIR)/service.cpp
LIB_OBJS := $(patsubst $(SRCDIR)/%.cpp,$(BUILDDIR)/%.o,$(LIB_SRCS))

BIN := sphdist
TEST_BIN := build/unit_tests

.PHONY: all test clean run-examples

all: $(BIN)

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/%.o: $(SRCDIR)/%.cpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): $(BUILDDIR)/main.o $(LIB_OBJS)
	$(CXX) $(CXXFLAGS) $^ -o $@

$(TEST_BIN): tests/unit_tests.cpp $(LIB_SRCS) | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) -I$(SRCDIR) $^ -o $@

# 先跑 C++ 单元测试，再跑 CLI/JSON 集成测试（Python）
test: $(TEST_BIN) $(BIN)
	@echo "=== C++ unit tests ==="
	$(TEST_BIN)
	@echo ""
	@echo "=== CLI integration tests ==="
	python3 tests/test_cli.py

run-examples: $(BIN)
	@bash examples/run_all.sh

clean:
	rm -rf $(BUILDDIR) $(BIN)
