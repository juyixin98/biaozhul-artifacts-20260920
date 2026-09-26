CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -O2
BUILD := build

LIB_OBJS := $(BUILD)/json.o $(BUILD)/cnf.o $(BUILD)/solver.o $(BUILD)/server.o

.PHONY: all test clean

all: $(BUILD)/sat_solver

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: src/%.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/sat_solver: src/main.cpp $(LIB_OBJS)
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_solver: tests/test_solver.cpp $(BUILD)/json.o $(BUILD)/cnf.o $(BUILD)/solver.o
	$(CXX) $(CXXFLAGS) -Isrc $^ -o $@

test: $(BUILD)/sat_solver $(BUILD)/test_solver
	$(BUILD)/test_solver
	python3 tests/test_api.py
