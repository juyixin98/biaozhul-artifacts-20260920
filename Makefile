# Makefile for the difference-constraints diagnostic backend.
CXX      ?= g++
# Note: -Wsign-conversion is intentionally omitted: vertices/edge indices are int
# throughout (bounded by MAX_VARIABLES/MAX_CONSTRAINTS) and index expressions
# dominate the code; the flag's noise would obscure meaningful warnings.
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow
CPPFLAGS ?= -Isrc
BUILDDIR := build
PYTHON   ?= python3

SOLVER_SRC := src/solver_service.cpp
SOLVER_HDR := src/json.hpp src/diff_constraints.hpp src/solver_service.hpp
TEST_SRC   := tests/test_solver.cpp

.PHONY: all test test-cpp test-py check sanitize clean

all: $(BUILDDIR)/diffcon_solver

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/diffcon_solver: src/main.cpp $(SOLVER_SRC) $(SOLVER_HDR) | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) src/main.cpp $(SOLVER_SRC) -o $@

$(BUILDDIR)/test_solver: $(TEST_SRC) $(SOLVER_SRC) $(SOLVER_HDR) | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(TEST_SRC) $(SOLVER_SRC) -o $@

test: test-cpp

# C++ unit tests alone.
test-cpp: $(BUILDDIR)/test_solver
	./$(BUILDDIR)/test_solver

# Full verification: C++ unit tests + Python end-to-end/property tests.
test-py: all
	$(PYTHON) tests/run_tests.py

check: test-cpp test-py

# Same C++ unit tests built with AddressSanitizer + UBSan.
sanitize: $(SOLVER_SRC) $(SOLVER_HDR) $(TEST_SRC) | $(BUILDDIR)
	$(CXX) -std=c++17 -g -O1 -fsanitize=address,undefined -fno-omit-frame-pointer \
	  $(CPPFLAGS) $(TEST_SRC) $(SOLVER_SRC) -o $(BUILDDIR)/test_sanitizer
	./$(BUILDDIR)/test_sanitizer

clean:
	rm -rf $(BUILDDIR)
