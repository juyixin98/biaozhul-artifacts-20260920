CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
SRCDIR   := src
BUILDDIR := build

LIB_SRCS := $(wildcard $(SRCDIR)/*.cpp)
LIB_OBJS := $(patsubst $(SRCDIR)/%.cpp,$(BUILDDIR)/%.o,$(filter-out $(SRCDIR)/main.cpp,$(LIB_SRCS)))
MAIN_OBJ := $(BUILDDIR)/main.o

BIN      := poly_locator
TESTBIN  := tests/run_tests

.PHONY: all test clean

all: $(BIN)

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/%.o: $(SRCDIR)/%.cpp $(SRCDIR)/*.hpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BIN): $(LIB_OBJS) $(MAIN_OBJ)
	$(CXX) $(CXXFLAGS) $^ -o $@

$(TESTBIN): tests/test_poly.cpp $(LIB_OBJS) $(SRCDIR)/*.hpp
	$(CXX) $(CXXFLAGS) -I$(SRCDIR) tests/test_poly.cpp $(LIB_OBJS) -o $@

test: $(BIN) $(TESTBIN)
	./$(TESTBIN)
	@bash tests/cli_tests.sh

clean:
	rm -rf $(BUILDDIR) $(BIN) $(TESTBIN)
