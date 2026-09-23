CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic

BIN := build/trajectory_interp
TESTBIN := build/test_trajectory

.PHONY: all test clean

all: $(BIN) $(TESTBIN)

build:
	mkdir -p build

$(BIN): src/main.cpp src/trajectory.cpp src/trajectory.hpp src/geometry.hpp src/json.hpp | build
	$(CXX) $(CXXFLAGS) src/main.cpp src/trajectory.cpp -o $@

$(TESTBIN): tests/test_main.cpp src/trajectory.cpp src/trajectory.hpp src/geometry.hpp | build
	$(CXX) $(CXXFLAGS) tests/test_main.cpp src/trajectory.cpp -o $@

test: $(TESTBIN)
	./$(TESTBIN)

clean:
	rm -rf build
