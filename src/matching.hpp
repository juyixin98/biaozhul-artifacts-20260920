// Bipartite maximum matching (Hopcroft-Karp) and minimum vertex cover
// derived via Kőnig's theorem. Implemented from scratch: no external solver.
#pragma once

#include <vector>

struct MatchingResult {
  int nLeft = 0;
  int nRight = 0;
  // matchLeft[u] = matched right vertex, or -1. Same for matchRight.
  std::vector<int> matchLeft;
  std::vector<int> matchRight;
  int matchingSize = 0;
  // Minimum vertex cover as a certificate of optimality (Kőnig's theorem):
  // coverLeft/coverRight mark which vertices belong to the cover.
  std::vector<bool> coverLeft;
  std::vector<bool> coverRight;
  int coverSize = 0;
};

// Computes a maximum matching and the corresponding minimum vertex cover.
// adj[u] lists the right-side neighbours of left vertex u (indices in
// [0, nRight)). Duplicate entries in adj are tolerated.
MatchingResult bipartiteMaxMatching(int nLeft, int nRight,
                                    const std::vector<std::vector<int>>& adj);

// Naive reference: exact maximum matching size by bitmask dynamic
// programming over the right side. Only usable for small nRight
// (nRight <= 24 is enforced; throws std::invalid_argument otherwise).
int naiveMaxMatchingSize(int nLeft, int nRight,
                         const std::vector<std::vector<int>>& adj);
