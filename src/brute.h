// Naive reference: exhaustive enumeration of every s-t cut.
//
// A cut is any node partition (S, T) with source in S and sink in T. There are
// exactly 2^(n-2) such cuts; this is exponential and intended ONLY for small
// graphs used as an independent oracle in tests/evidence. Parallel edges are
// summed naturally (each edge contributes independently), zero-capacity
// edges contribute zero, and self-loops never cross a partition.
#pragma once

#include <vector>

#include "network.h"

namespace mincut {

struct BruteResult {
  long long min_cut_value = 0;
  std::vector<char> source_side;  // witness partition achieving the minimum
  long long partitions_checked = 0;
};

// Computes the minimum cut value by complete enumeration. Requires
// 2^(n-2) to be tractable; callers must enforce the size limit.
BruteResult bruteForceMinCut(const Network& net);

// Cut capacity of a single partition: sum of capacities of edges u->v with
// u in S and v not in S. Exposed for testing.
long long cutCapacity(const Network& net, const std::vector<char>& source_side);

}  // namespace mincut
