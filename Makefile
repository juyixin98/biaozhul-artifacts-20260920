CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
DEPFLAGS := -MMD -MP

BIN := dagpaths
OBJ := build/main.o build/dag.o build/json.o build/bigint.o
TEST_BIN := build/test_bigint

.PHONY: all test clean

all: $(BIN)

$(BIN): $(OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $(OBJ)

build/%.o: src/%.cpp | build
	$(CXX) $(CXXFLAGS) $(DEPFLAGS) -c -o $@ $<

-include $(OBJ:.o=.d)

build:
	mkdir -p build

$(TEST_BIN): tests/test_bigint.cpp build/bigint.o src/bigint.hpp | build
	$(CXX) $(CXXFLAGS) -o $@ $< build/bigint.o

test: $(BIN) $(TEST_BIN)
	./$(TEST_BIN)
	bash tests/run_tests.sh
	python3 tests/random_check.py

clean:
	rm -rf build $(BIN)
