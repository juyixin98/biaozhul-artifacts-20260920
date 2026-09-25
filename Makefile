CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
INCLUDE := -Iinclude

BIN := build/traj_interp
TESTBIN := build/test_interp

.PHONY: all test clean

all: $(BIN) $(TESTBIN)

$(BIN): src/main.cpp include/traj/quat.hpp include/traj/trajectory.hpp include/traj/json.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $(INCLUDE) -o $@ src/main.cpp

$(TESTBIN): tests/test_interp.cpp include/traj/quat.hpp include/traj/trajectory.hpp include/traj/json.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $(INCLUDE) -o $@ tests/test_interp.cpp

test: $(TESTBIN)
	./$(TESTBIN)

clean:
	rm -rf build
