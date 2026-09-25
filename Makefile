# Spatial nearest-neighbour index — backend only.
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
LDFLAGS  ?=

BIN      := spatial_index
SRC      := src/main.cpp
HEADERS  := src/kdtree.hpp src/json.hpp
UNIT_BIN := tests/test_kdtree

.PHONY: all test clean

all: $(BIN)

$(BIN): $(SRC) $(HEADERS)
	$(CXX) $(CXXFLAGS) -Isrc $(SRC) -o $@ $(LDFLAGS)

$(UNIT_BIN): tests/test_kdtree.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -Isrc tests/test_kdtree.cpp -o $@ $(LDFLAGS)

test: $(BIN) $(UNIT_BIN)
	$(UNIT_BIN)
	python3 tests/test_differential.py

clean:
	rm -f $(BIN) $(UNIT_BIN)
