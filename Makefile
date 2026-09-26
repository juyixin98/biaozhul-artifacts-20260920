CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
TARGET    = diffc
SRCS      = src/main.cpp src/json.cpp src/model.cpp src/solver.cpp \
            src/reference.cpp src/diagnose.cpp
OBJS      = $(SRCS:.cpp=.o)

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $(OBJS)

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -c -o $@ $<

# Header dependencies (kept simple and explicit).
src/main.o:      src/main.cpp src/json.hpp src/model.hpp src/solver.hpp src/reference.hpp src/diagnose.hpp
src/json.o:      src/json.cpp src/json.hpp
src/model.o:     src/model.cpp src/model.hpp src/json.hpp
src/solver.o:    src/solver.cpp src/solver.hpp src/model.hpp src/json.hpp
src/reference.o: src/reference.cpp src/reference.hpp src/model.hpp src/json.hpp
src/diagnose.o:  src/diagnose.cpp src/diagnose.hpp src/model.hpp src/json.hpp src/solver.hpp src/reference.hpp

test: $(TARGET)
	./tests/run_tests.sh

clean:
	rm -f $(TARGET) $(OBJS)

.PHONY: all test clean
