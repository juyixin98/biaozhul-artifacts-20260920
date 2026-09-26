#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace mcut {

// One directed edge of the input graph. Parallel edges are simply separate
// InputEdge records (they must have distinct ids). Capacity is a nonnegative
// integer.
struct InputEdge {
  std::string id;
  int from = -1;
  int to = -1;
  std::int64_t capacity = 0;
};

struct Problem {
  int num_vertices = 0;
  int source = -1;
  int sink = -1;
  std::vector<InputEdge> edges;
};

}  // namespace mcut
