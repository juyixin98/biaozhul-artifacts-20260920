#include "bruteforce.hpp"

#include <limits>

namespace mcut {

BruteForceResult brute_force_min_cut(const Problem& problem) {
  const int n = problem.num_vertices;
  const int s = problem.source;
  const int t = problem.sink;

  // Vertices other than s and t get independent bits.
  std::vector<int> others;
  others.reserve(n - 2);
  for (int v = 0; v < n; ++v) {
    if (v != s && v != t) others.push_back(v);
  }
  const int free_count = static_cast<int>(others.size());

  BruteForceResult result;
  result.min_cut_value = std::numeric_limits<std::int64_t>::max();
  result.source_side.assign(n, 0);
  result.source_side[s] = 1;

  const std::uint64_t total =
      std::uint64_t{1} << free_count;  // caller bounds free_count <= 16

  std::vector<unsigned char> side(n, 0);
  side[s] = 1;
  side[t] = 0;

  for (std::uint64_t mask = 0; mask < total; ++mask) {
    for (int i = 0; i < free_count; ++i) {
      side[others[i]] = (mask >> i) & 1ULL;
    }
    std::int64_t value = 0;
    for (const InputEdge& e : problem.edges) {
      if (side[e.from] && !side[e.to]) value += e.capacity;
    }
    ++result.partitions_checked;
    if (value < result.min_cut_value) {
      result.min_cut_value = value;
      result.source_side = side;
    }
  }
  return result;
}

}  // namespace mcut
