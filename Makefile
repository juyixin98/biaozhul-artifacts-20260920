CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow
LDFLAGS  ?=

SRC      := src/json.cpp src/graph_builder.cpp src/scc.cpp src/naive.cpp src/verify.cpp
OBJ      := $(SRC:.cpp=.o)
BIN      := scc_analyze
TEST_BIN := run_tests

.PHONY: all clean test pytest check

all: $(BIN)

$(BIN): $(OBJ) src/main.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)

src/%.o: src/%.cpp
	$(CXX) $(CXXFLAGS) -c -o $@ $<

$(TEST_BIN): $(OBJ) tests/test_scc.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)

tests/test_scc.o: tests/test_scc.cpp
	$(CXX) $(CXXFLAGS) -I src -c -o $@ tests/test_scc.cpp

# C++ unit tests
test: $(TEST_BIN)
	./$(TEST_BIN)

# Python end-to-end + differential tests
pytest:
	python3 tests/run_e2e_tests.py

check: test pytest

clean:
	rm -f $(OBJ) src/main.o tests/test_scc.o $(BIN) $(TEST_BIN)
