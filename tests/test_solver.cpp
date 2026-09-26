// test_solver.cpp - Unit tests for the difference-constraints core.
//
// Covers: feasible/infeasible systems, disconnected variables and isolated
// vertices, zero-weight cycles, per-component normalization, irreducibility of
// the minimal contradiction candidate, and randomized Bellman-Ford vs naive
// Floyd-Warshall agreement.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <random>
#include <set>
#include <string>
#include <vector>

#include "diff_constraints.hpp"
#include "solver_service.hpp"

using namespace diffcon;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& name) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::cerr << "FAIL: " << name << "\n";
  } else {
    std::cout << "ok:   " << name << "\n";
  }
}

std::vector<Constraint> edgesFromTriples(const std::vector<std::array<int64_t, 3>>& t) {
  std::vector<Constraint> e;
  for (auto [x, y, c] : t) e.push_back({static_cast<int>(x), static_cast<int>(y), c});
  return e;
}

// Exhaustive feasibility for tiny systems by enumerating a bounded box.
// A feasible difference-constraints system with bounds in [-C,C] always has a
// representative solution in a range of size O(n*C); here we use [-m*C, m*C].
bool bruteForceFeasible(int n, const std::vector<Constraint>& edges, int64_t C,
                        std::vector<int64_t>* witness = nullptr) {
  int64_t lo = -static_cast<int64_t>(edges.size()) * C;
  int64_t hi = static_cast<int64_t>(edges.size()) * C;
  std::vector<int64_t> v(n, lo);
  while (true) {
    bool good = true;
    for (const Constraint& e : edges) {
      if (v[e.x] - v[e.y] > e.c) { good = false; break; }
    }
    if (good) {
      if (witness) *witness = v;
      return true;
    }
    int i = 0;
    while (i < n) {
      if (v[i] < hi) { ++v[i]; break; }
      v[i] = lo;
      ++i;
    }
    if (i == n) return false;
  }
}

void testSimpleFeasible() {
  // x - y <= 2 ; y - x <= 1  ->  -1 <= x - y <= 2, feasible
  auto e = edgesFromTriples({{0, 1, 2}, {1, 0, 1}});
  auto r = bellmanFord(2, e);
  check(r.feasible, "simple system feasible");
  check(assignmentSatisfies(r.dist, e), "BF labels satisfy simple system");

  auto fw = floydWarshall(2, e);
  check(fw.used && fw.feasible, "Floyd agrees simple system feasible");
}

void testDisconnected() {
  // Component A: 0-1<=3, 1-0<=-3 (x0-x1==3). Component B: 2-3<=-5.
  // Vertex 4 isolated.
  auto e = edgesFromTriples({{0, 1, 3}, {1, 0, -3}, {2, 3, -5}});
  auto r = bellmanFord(5, e);
  check(r.feasible, "disconnected system feasible");
  check(assignmentSatisfies(r.dist, e), "BF labels satisfy disconnected system");
  check(r.dist[4] == 0, "isolated vertex label is 0");

  auto comps = connectedComponents(5, e);
  check(comps.size() == 3, "three components (two linked + one isolated)");
  check(comps[2].size() == 1 && comps[2][0] == 4, "isolated vertex forms singleton component");

  auto norm = normalizePerComponent(r.dist, comps);
  check(assignmentSatisfies(norm, e), "normalized labels still satisfy constraints");
  std::set<int64_t> mins;
  for (const auto& comp : comps) {
    int64_t mn = INT64_MAX;
    for (int v : comp) mn = std::min(mn, norm[v]);
    mins.insert(mn);
  }
  check(mins.size() == 1 && *mins.begin() == 0, "every component normalized to min 0");
  // Relative difference inside component A preserved: x0 - x1 == 3.
  check(norm[0] - norm[1] == 3, "normalization preserves relative difference");
}

void testZeroCycle() {
  // x-y<=1, y-z<=1, z-x<=-2 : cycle sum 0 -> feasible, forces x-y==1,y-z==1.
  auto e = edgesFromTriples({{0, 1, 1}, {1, 2, 1}, {2, 0, -2}});
  auto r = bellmanFord(3, e);
  check(r.feasible, "zero-sum cycle is feasible");
  check(assignmentSatisfies(r.dist, e), "zero-cycle labels satisfy all constraints");
  check(r.dist[0] - r.dist[1] == 1 && r.dist[1] - r.dist[2] == 1,
        "zero cycle tight constraints hold with equality");
  auto fw = floydWarshall(3, e);
  check(fw.feasible, "Floyd agrees zero cycle feasible");
}

void testNegativeCycleWitness() {
  // x-y<=1, y-z<=1, z-x<=-4 -> sum -1 < 0 infeasible; extra unrelated edge.
  auto e = edgesFromTriples({{0, 1, 1}, {1, 2, 1}, {2, 0, -4}, {3, 4, 0}});
  auto r = bellmanFord(5, e);
  check(!r.feasible, "negative cycle detected");
  check(r.cycleEdges.size() == 3, "witness has exactly the three cycle edges");
  check(r.cycleWeight < 0, "reported cycle weight is negative");
  int64_t sum = 0;
  for (int ei : r.cycleEdges) sum += e[ei].c;
  check(sum == r.cycleWeight && sum == -2, "cycle weight sums bounds correctly");
  check(r.cycleVertices.front() == r.cycleVertices.back(), "vertex cycle closes");

  auto fw = floydWarshall(5, e);
  check(!fw.feasible, "Floyd agrees negative cycle infeasible");
}

void testMinimalCandidate() {
  // 4 constraints: the 3-edge negative cycle plus one redundant-but-needed?
  // Build two overlapping cycles so naive witness is not the MUS.
  // edges: a-b<=0(0), b-c<=0(1), c-a<=-1(2) -> cycle sum -1
  //        b-d<=0(3), d-c<=-2(4): b->d->c->b via edge1 reversed? c->b is -edge1?
  // Keep it simple: cycle1 = {0,1,2}; edge 3 is a harmless extra (a-d<=5).
  auto e = edgesFromTriples({{0, 1, 0}, {1, 2, 0}, {2, 0, -1}, {0, 3, 5}});
  auto r = bellmanFord(4, e);
  check(!r.feasible, "fixture infeasible");
  auto mc = minimalContradictionCandidate(4, e, r.cycleEdges);
  check(mc.method == "deletion_filter", "deletion filter used at small scale");
  std::set<int> members(mc.edgeIndices.begin(), mc.edgeIndices.end());
  check(members == std::set<int>({0, 1, 2}), "MUS candidate is exactly the 3-cycle edges");

  // Irreducible: candidate infeasible, every one-edge deletion feasible.
  auto sub = [&](const std::vector<int>& ids) {
    std::vector<Constraint> s;
    for (int i : ids) s.push_back(e[i]);
    return bellmanFord(4, s).feasible;
  };
  check(!sub(mc.edgeIndices), "MUS candidate itself infeasible");
  bool irreducible = true;
  for (int i : mc.edgeIndices) {
    std::vector<int> rest = mc.edgeIndices;
    rest.erase(std::find(rest.begin(), rest.end(), i));
    if (!sub(rest)) irreducible = false;
  }
  check(irreducible, "MUS candidate is irreducible under single-edge removal");
}

void testTwoCyclesMUS() {
  // Two disjoint negative cycles sharing one edge creates a smaller unique MUS
  // only one cycle; instead verify deletion-filter picks one whole cycle when
  // two disjoint cycles exist (order dependent).
  auto e = edgesFromTriples({
      {0, 1, 1}, {1, 0, -3},   // cycle sum -2 on {0,1}
      {2, 3, 1}, {3, 2, -3}}); // cycle sum -2 on {2,3}
  auto r = bellmanFord(4, e);
  check(!r.feasible, "two disjoint cycles infeasible");
  auto mc = minimalContradictionCandidate(4, e, r.cycleEdges);
  check(mc.edgeIndices.size() == 2, "MUS candidate isolates one of the two 2-cycles");
  std::set<int> members(mc.edgeIndices.begin(), mc.edgeIndices.end());
  check((members == std::set<int>({0, 1}) || members == std::set<int>({2, 3})),
        "candidate is one of the disjoint cycles");
}

void testRandomizedAgreement() {
  std::mt19937 rng(12345);
  int trials = 300;
  int infeasibleCount = 0;
  bool allMatch = true;
  bool witnessesValid = true;
  bool labelsOk = true;
  for (int t = 0; t < trials; ++t) {
    int n = 1 + static_cast<int>(rng() % 7);
    int m = static_cast<int>(rng() % (2 * n + 3));
    std::vector<Constraint> e;
    for (int k = 0; k < m; ++k) {
      int x = static_cast<int>(rng() % n);
      int y = static_cast<int>(rng() % n);
      int64_t c = static_cast<int64_t>(rng() % 7) - 3;  // [-3,3]
      e.push_back({x, y, c});
    }
    BellmanFordResult bf = bellmanFord(n, e);
    FloydResult fw = floydWarshall(n, e);
    if (bf.feasible != fw.feasible) { allMatch = false; continue; }
    if (bf.feasible) {
      if (!assignmentSatisfies(bf.dist, e)) labelsOk = false;
      // Cross-check against exhaustive enumeration for the smallest cases.
      if (n <= 3 && m <= 5) {
        bool brute = bruteForceFeasible(n, e, 3);
        if (brute != bf.feasible) allMatch = false;
      }
    } else {
      ++infeasibleCount;
      if (bf.cycleEdges.empty() || bf.cycleWeight >= 0) witnessesValid = false;
      int64_t sum = 0;
      for (int ei : bf.cycleEdges) sum += e[ei].c;
      if (sum >= 0 || bf.cycleVertices.front() != bf.cycleVertices.back()) witnessesValid = false;
    }
  }
  check(allMatch, "random: Bellman-Ford and Floyd (and brute force) agree on feasibility");
  check(labelsOk, "random: all feasible BF labels satisfy every constraint");
  check(witnessesValid, "random: every infeasible case has a valid closed negative witness");
  check(infeasibleCount > 30, "random: enough infeasible cases were generated");
}

void testEmptySystem() {
  std::vector<Constraint> e;
  auto r = bellmanFord(0, e);
  check(r.feasible, "empty variable set is feasible");
  auto comps = connectedComponents(0, e);
  check(comps.empty(), "empty system has no components");
}

void testEarlyExit() {
  // No constraints: round 1 changes nothing.
  auto r = bellmanFord(3, {});
  check(r.feasible && r.iterations == 1, "unconstrained variables converge in one round");
}

// ---------- JSON service-level tests ----------

std::string getField(const json::Value& v, const std::string& key) {
  const json::Value* f = v.find(key);
  return f ? f->asString() : std::string("<missing>");
}

void testServiceFeasible() {
  std::string req = R"({
    "variables": ["x", "y", "z", "lonely"],
    "constraints": [
      {"id": "c1", "x": "x", "y": "y", "c": 2},
      {"id": "c2", "x": "y", "y": "x", "c": 1},
      {"id": "c3", "x": "y", "y": "z", "c": -1}
    ]
  })";
  json::Value resp = handleSolveRequest(req);
  check(getField(resp, "status") == "ok", "service: feasible request returns ok");
  check(resp.find("feasible") && resp.find("feasible")->asBool(), "service: feasible flag true");
  const json::Value* ver = resp.find("verification");
  check(ver && ver->find("all_satisfied")->asBool(), "service: response verification all_satisfied");
  const json::Value* ref = resp.find("evidence")->find("reference");
  check(ref->find("verdict_matches_primary")->asBool(), "service: Floyd reference matches");
  check(ref->find("floyd_assignment_verified")->asBool(), "service: Floyd assignment verified");
  const json::Value* a = resp.find("assignment");
  check(a->find("lonely")->asInt() == 0, "service: isolated variable normalized to 0");
}

void testServiceInfeasible() {
  std::string req = R"({
    "variables": ["x", "y", "z"],
    "constraints": [
      {"id": "alpha", "x": "x", "y": "y", "c": 1},
      {"id": "beta",  "x": "y", "y": "z", "c": 1},
      {"id": "gamma", "x": "z", "y": "x", "c": -4}
    ]
  })";
  json::Value resp = handleSolveRequest(req);
  check(getField(resp, "status") == "ok", "service: infeasible request still status ok");
  check(!resp.find("feasible")->asBool(), "service: feasible flag false");
  const json::Value* cyc = resp.find("negative_cycle");
  check(cyc && cyc->find("sum_bounds")->asInt() < 0, "service: cycle sum negative");
  check(cyc->find("closes")->asBool(), "service: cycle closes");
  const json::Value* ids = cyc->find("constraint_ids");
  std::set<std::string> got;
  for (const auto& v : ids->asArray()) got.insert(v.asString());
  check(got == std::set<std::string>({"alpha", "beta", "gamma"}),
        "service: witness names the exact constraint IDs");
  const json::Value* mc = resp.find("minimal_candidate");
  check(mc && mc->find("verification")->find("irreducible")->asBool(),
        "service: minimal candidate verified irreducible");
}

void testServiceErrors() {
  auto resp1 = handleSolveRequest("{not json");
  check(getField(resp1, "status") == "error", "service: malformed JSON rejected");

  std::string req2 = R"({"variables": ["x", "x"], "constraints": []})";
  check(getField(handleSolveRequest(req2), "status") == "error", "service: duplicate variable rejected");

  std::string req3 = R"({"constraints": [{"x": "x", "y": "y", "c": 1.5}]})";
  check(getField(handleSolveRequest(req3), "status") == "error", "service: float bound rejected");

  std::string req4 = R"({"constraints": [{"x": "x", "y": "y", "c": 1},
                                          {"id": "k", "x": "y", "y": "x", "c": 0},
                                          {"id": "k", "x": "x", "y": "y", "c": 0}]})";
  check(getField(handleSolveRequest(req4), "status") == "error", "service: duplicate id rejected");

  std::string req5 = "[]";
  check(getField(handleSolveRequest(req5), "status") == "error", "service: non-object body rejected");
}

}  // namespace

int main() {
  testSimpleFeasible();
  testDisconnected();
  testZeroCycle();
  testNegativeCycleWitness();
  testMinimalCandidate();
  testTwoCyclesMUS();
  testRandomizedAgreement();
  testEmptySystem();
  testEarlyExit();
  testServiceFeasible();
  testServiceInfeasible();
  testServiceErrors();

  std::cout << "\n" << (g_failures == 0 ? "ALL TESTS PASSED" : "SOME TESTS FAILED")
            << " (" << g_checks << " checks, " << g_failures << " failures)\n";
  return g_failures == 0 ? 0 : 1;
}
