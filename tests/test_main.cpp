// Test harness: unit tests plus randomized cross-validation of the
// Hopcroft-Karp solver against the naive exhaustive reference, and
// verification of the vertex-cover certificate on every instance.
#include <cstdlib>
#include <functional>
#include <iostream>
#include <random>
#include <set>
#include <string>
#include <vector>

#include "matching.hpp"

namespace {

int gFailures = 0;
int gChecks = 0;

void check(bool cond, const std::string& what) {
  ++gChecks;
  if (!cond) {
    ++gFailures;
    std::cerr << "FAIL: " << what << "\n";
  }
}

// Kuhn's augmenting-path algorithm as an independent mid-scale reference
// (different algorithm from Hopcroft-Karp, cheap to write correctly).
int kuhnMaxMatchingSize(int nLeft, int nRight,
                        const std::vector<std::vector<int>>& adj) {
  std::vector<int> matchR(nRight, -1);
  int size = 0;
  for (int u = 0; u < nLeft; ++u) {
    std::vector<bool> seen(nRight, false);
    std::function<bool(int)> tryAugment = [&](int x) -> bool {
      for (int v : adj[x]) {
        if (seen[v]) continue;
        seen[v] = true;
        if (matchR[v] == -1 || tryAugment(matchR[v])) {
          matchR[v] = x;
          return true;
        }
      }
      return false;
    };
    if (tryAugment(u)) ++size;
  }
  return size;
}

void verifyCertificate(int nL, int nR, const std::vector<std::vector<int>>& adj,
                       const MatchingResult& r, const std::string& ctx) {
  // 1. Matching is valid: edges exist, no vertex used twice, arrays agree.
  std::set<std::pair<int, int>> edgeSet;
  for (int u = 0; u < nL; ++u)
    for (int v : adj[u]) edgeSet.insert({u, v});
  std::vector<bool> usedL(nL, false), usedR(nR, false);
  int counted = 0;
  bool valid = true;
  for (int u = 0; u < nL && valid; ++u) {
    int v = r.matchLeft[u];
    if (v < 0) continue;
    ++counted;
    if (v >= nR || usedL[u] || usedR[v] || r.matchRight[v] != u ||
        !edgeSet.count({u, v})) {
      valid = false;
    }
    usedL[u] = usedR[v] = true;
  }
  check(valid, ctx + ": matching is valid");
  check(counted == r.matchingSize, ctx + ": matchingSize consistent");

  // 2. Cover covers every edge.
  bool covers = true;
  for (int u = 0; u < nL && covers; ++u)
    for (int v : adj[u])
      if (!r.coverLeft[u] && !r.coverRight[v]) covers = false;
  check(covers, ctx + ": vertex cover covers all edges");

  // 3. |cover| == |matching| (Kőnig) — optimality certificate.
  check(r.coverSize == r.matchingSize,
        ctx + ": |cover| == |matching| (cover=" + std::to_string(r.coverSize) +
            " matching=" + std::to_string(r.matchingSize) + ")");
}

std::vector<std::vector<int>> toAdj(int nL,
                                    const std::vector<std::pair<int, int>>& edges) {
  std::vector<std::vector<int>> adj(nL);
  for (auto& e : edges) adj[e.first].push_back(e.second);
  return adj;
}

void unitTests() {
  // Empty graph.
  {
    auto r = bipartiteMaxMatching(0, 0, {});
    check(r.matchingSize == 0 && r.coverSize == 0, "empty graph");
  }
  // Isolated vertices only, no edges.
  {
    auto r = bipartiteMaxMatching(3, 2, {{}, {}, {}});
    check(r.matchingSize == 0 && r.coverSize == 0, "isolated vertices");
  }
  // Single edge.
  {
    auto r = bipartiteMaxMatching(1, 1, {{0}});
    check(r.matchingSize == 1 && r.coverSize == 1, "single edge");
    check(r.matchLeft[0] == 0 && r.matchRight[0] == 0, "single edge pairing");
  }
  // Duplicate edges collapse to one.
  {
    auto r = bipartiteMaxMatching(1, 1, {{0, 0, 0}});
    check(r.matchingSize == 1, "duplicate edges");
  }
  // Path of length 3: L0-R0, L1-R0, L1-R1 -> matching 2.
  {
    auto adj = toAdj(2, {{0, 0}, {1, 0}, {1, 1}});
    auto r = bipartiteMaxMatching(2, 2, adj);
    check(r.matchingSize == 2, "path graph matching");
    verifyCertificate(2, 2, adj, r, "path graph");
  }
  // Complete bipartite K(3,3).
  {
    auto adj = toAdj(3, {{0,0},{0,1},{0,2},{1,0},{1,1},{1,2},{2,0},{2,1},{2,2}});
    auto r = bipartiteMaxMatching(3, 3, adj);
    check(r.matchingSize == 3 && r.coverSize == 3, "K(3,3)");
    verifyCertificate(3, 3, adj, r, "K(3,3)");
  }
  // Star: one left vertex to many right -> matching 1.
  {
    auto adj = toAdj(1, {{0, 0}, {0, 1}, {0, 2}, {0, 3}});
    auto r = bipartiteMaxMatching(1, 4, adj);
    check(r.matchingSize == 1 && r.coverSize == 1, "star");
    verifyCertificate(1, 4, adj, r, "star");
  }
  // Naive reference sanity: K(2,2) = 2, path above = 2, empty = 0.
  check(naiveMaxMatchingSize(2, 2, toAdj(2, {{0,0},{0,1},{1,0},{1,1}})) == 2,
        "naive K(2,2)");
  check(naiveMaxMatchingSize(2, 2, toAdj(2, {{0,0},{1,0},{1,1}})) == 2,
        "naive path");
  check(naiveMaxMatchingSize(3, 2, {{}, {}, {}}) == 0, "naive empty");
}

void randomizedSmallTests(std::mt19937& rng, int trials) {
  for (int t = 0; t < trials; ++t) {
    int nL = rng() % 9;              // 0..8 left vertices
    int nR = rng() % 9;              // 0..8 right vertices
    std::vector<std::pair<int, int>> edges;
    if (nL > 0 && nR > 0) {
      int m = rng() % (nL * nR + 2);  // may exceed nL*nR -> duplicates
      for (int i = 0; i < m; ++i) {
        edges.emplace_back(rng() % nL, rng() % nR);  // duplicates possible
      }
    }
    auto adj = toAdj(nL, edges);
    auto r = bipartiteMaxMatching(nL, nR, adj);
    int expected = naiveMaxMatchingSize(nL, nR, adj);
    check(r.matchingSize == expected,
          "random small #" + std::to_string(t) + ": HK=" +
              std::to_string(r.matchingSize) +
              " naive=" + std::to_string(expected) + " nL=" +
              std::to_string(nL) + " nR=" + std::to_string(nR));
    verifyCertificate(nL, nR, adj, r,
                      "random small #" + std::to_string(t));
  }
}

void randomizedMediumTests(std::mt19937& rng, int trials) {
  for (int t = 0; t < trials; ++t) {
    int nL = 1 + rng() % 60;
    int nR = 1 + rng() % 60;
    std::vector<std::pair<int, int>> edges;
    int m = rng() % (nL * nR / 2 + 1);
    for (int i = 0; i < m; ++i) {
      edges.emplace_back(rng() % nL, rng() % nR);
    }
    auto adj = toAdj(nL, edges);
    auto r = bipartiteMaxMatching(nL, nR, adj);
    int expected = kuhnMaxMatchingSize(nL, nR, adj);
    check(r.matchingSize == expected,
          "random medium #" + std::to_string(t) + ": HK=" +
              std::to_string(r.matchingSize) +
              " kuhn=" + std::to_string(expected));
    verifyCertificate(nL, nR, adj, r,
                      "random medium #" + std::to_string(t));
  }
}

}  // namespace

int main(int argc, char** argv) {
  unsigned seed = (argc > 1) ? std::stoul(argv[1]) : 12345u;
  std::mt19937 rng(seed);

  unitTests();
  randomizedSmallTests(rng, 2000);
  randomizedMediumTests(rng, 500);

  std::cout << "checks: " << gChecks << ", failures: " << gFailures
            << " (seed " << seed << ")\n";
  return gFailures == 0 ? 0 : 1;
}
