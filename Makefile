CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow
BUILDDIR := build
SRC := src/json.cpp src/graph.cpp src/treewidth.cpp src/validate.cpp
OBJ := $(SRC:src/%.cpp=$(BUILDDIR)/%.o)

.PHONY: all test clean check e2e

all: $(BUILDDIR)/treewidth

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/%.o: src/%.cpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) -Isrc -c $< -o $@

$(BUILDDIR)/treewidth: $(OBJ) src/cli.cpp
	$(CXX) $(CXXFLAGS) -Isrc src/cli.cpp $(OBJ) -o $@

$(BUILDDIR)/tw_test: $(OBJ) tests/test.cpp
	$(CXX) $(CXXFLAGS) -Isrc tests/test.cpp $(OBJ) -o $@

$(BUILDDIR)/exhaustive: $(OBJ) tools/exhaustive.cpp
	$(CXX) $(CXXFLAGS) -Isrc tools/exhaustive.cpp $(OBJ) -o $@

exhaustive: $(BUILDDIR)/exhaustive
	./$(BUILDDIR)/exhaustive 7

test: $(BUILDDIR)/tw_test
	./$(BUILDDIR)/tw_test

e2e: all
	./tests/e2e.sh

check: test e2e

clean:
	rm -rf $(BUILDDIR)
