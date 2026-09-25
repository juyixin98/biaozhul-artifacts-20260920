# rect_union — axis-aligned rectangle union backend
#
#   make            build the CLI/HTTP binary at build/rect_union
#   make test       build and run unit tests (built-in framework, no deps)
#   make check      unit tests + black-box integration tests
#   make clean

CXX      ?= g++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
LDFLAGS  ?=

BUILD := build
BIN   := $(BUILD)/rect_union
UTBIN := $(BUILD)/unit_tests

SRCS := src/main.cpp src/app.cpp

.PHONY: all test check clean

all: $(BIN)

$(BIN): $(SRCS) src/sweep.hpp src/json.hpp src/app.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SRCS) -o $@ $(LDFLAGS)

$(UTBIN): tests/test_main.cpp tests/test_sweep.cpp tests/test_app.cpp \
          tests/framework.hpp src/sweep.hpp src/json.hpp src/app.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Wno-sign-conversion -Wno-conversion \
	    tests/test_main.cpp tests/test_sweep.cpp tests/test_app.cpp \
	    src/app.cpp -o $@ $(LDFLAGS)

$(BUILD):
	mkdir -p $(BUILD)

test: $(UTBIN)
	./$(UTBIN)

check: $(UTBIN) $(BIN)
	./$(UTBIN)
	bash tests/integration.sh

clean:
	rm -rf $(BUILD)
