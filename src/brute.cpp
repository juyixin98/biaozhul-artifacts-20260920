#include "brute.h"

#include <cstdint>

namespace mincut {

long long cutCapacity(const Network& net, const std::vector<char>& source_side) {
  long long sum = 0;
  for (const InputEdge& e : net.edges) {
    // Parallel edges each contribute independently; zero-capacity edges
    // contribute zero; self-loops never cross the partition.
    if (source_side[e.from] && !source_side[e.to]) sum += e.capacity;
  }
  return sum;
}

BruteResult bruteForceMinCut(const Network& net) {
  const int n = static_cast<int>(net.node_names.size());
  BruteResult result;
  result.source_side.assign(n, 0);
  result.source_side[net.source] = 1;

  // Free nodes are every node except source and sink, in index order.
  std::vector<int> free_nodes;
  free_nodes.reserve(n - 2);
  for (int v = 0; v < n; ++v) {
    if (v != net.source && v != net.sink) free_nodes.push_back(v);
  }

  // Enumerate 2^(n-2) assignments of the non-terminal nodes via bitmask.
  const std::uint64_t total = free_nodes.empty()
                                  ? 1ULL
                                  : (1ULL << static_cast<unsigned>(free_nodes.size()));
  long long best = 0;
  for (std::uint64_t mask = 0; mask < total; ++mask) {
    std::vector<char> side(n, 0);
    side[net.source] = 1;
    for (std::size_t k = 0; k < free_nodes.size(); ++k) {
      if ((mask >> k) & 1ULL) side[free_nodes[k]] = 1;
    }
    long long value = cutCapacity(net, side);
    ++result.partitions_checked;
    if (mask == 0 || value < best) {
      best = value;
      result.min_cut_value = value;
      result.source_side = side;
    }
  }
  return result;
}

}  // namespace mincut
