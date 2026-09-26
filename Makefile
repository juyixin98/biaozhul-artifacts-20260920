CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic

SRC := src/main.cpp src/cnf.cpp src/dpll.cpp src/reference.cpp src/checker.cpp
HDR := src/json.hpp src/cnf.hpp src/dpll.hpp src/reference.hpp src/checker.hpp
BIN := bin/sat_backend

all: $(BIN)

$(BIN): $(SRC) $(HDR)
	mkdir -p bin
	$(CXX) $(CXXFLAGS) -o $@ $(SRC)

test: all
	python3 tests/run_tests.py

clean:
	rm -rf bin

.PHONY: all test clean
