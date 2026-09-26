CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Werror

BUILD := build
CORE := src/json.cpp src/matching.cpp src/api.cpp

all: $(BUILD)/bmatch $(BUILD)/run_tests

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/bmatch: $(CORE) src/main.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ $(CORE) src/main.cpp

$(BUILD)/run_tests: src/matching.cpp tests/test_main.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -Isrc -o $@ src/matching.cpp tests/test_main.cpp

test: all
	./tests/run_tests.sh

clean:
	rm -rf $(BUILD)

.PHONY: all test clean
