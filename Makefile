# raybox — offline 3D ray / AABB intersection backend
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion
BUILDDIR  := build
SRC       := src/vec3.h src/ray_aabb.h src/bvh.h src/json.h src/api.h \
             src/ray_aabb.cc src/bvh.cc src/json.cc src/api.cc src/main.cc
LIBOBJ    := $(BUILDDIR)/ray_aabb.o $(BUILDDIR)/bvh.o $(BUILDDIR)/json.o $(BUILDDIR)/api.o

.PHONY: all test clean run-test

all: $(BUILDDIR)/raybox

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/%.o: src/%.cc | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) -Isrc -c $< -o $@

$(BUILDDIR)/raybox: $(LIBOBJ) $(BUILDDIR)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILDDIR)/test_raybox: $(LIBOBJ) tests/test_raybox.cc
	$(CXX) $(CXXFLAGS) -Isrc tests/test_raybox.cc $(LIBOBJ) -o $@

# C++ geometry/BVH unit tests
test: $(BUILDDIR)/test_raybox
	$(BUILDDIR)/test_raybox

# Full suite: unit tests + CLI/JSON end-to-end tests
run-test: $(BUILDDIR)/raybox $(BUILDDIR)/test_raybox
	$(BUILDDIR)/test_raybox
	python3 tests/test_cli.py ./$(BUILDDIR)/raybox

clean:
	rm -rf $(BUILDDIR)
