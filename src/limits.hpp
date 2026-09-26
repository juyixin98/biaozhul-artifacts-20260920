#pragma once

namespace mcut {

// Hard scale limits of this backend. They also guarantee that no int64_t
// accumulation can overflow: maximum possible flow value is bounded by
// kMaxEdges * kMaxCapacity = 1e14, far below INT64_MAX.
constexpr int kMaxVertices = 10000;
constexpr int kMaxEdges = 100000;
constexpr long long kMaxCapacity = 1'000'000'000LL;

// The naive reference enumerates all 2^(n-2) s-t partitions, so it is only
// accepted on small instances.
constexpr int kBruteMaxVertices = 18;
constexpr int kBruteMaxEdges = 2000;

}  // namespace mcut
