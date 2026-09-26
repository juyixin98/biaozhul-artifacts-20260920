// C++ unit tests: Dinic vs exhaustive brute force on fixed and randomized
// networks, including parallel edges, zero capacities, self loops, opposite
// edges and disconnected nodes. No third-party test framework.
#include <algorithm>
#include <cstdint>
#include <iostream>
#include <random>
#include <string>
#include <vector>

#include "brute.h"
#include "dinic.h"
#include "minjson.h"
#include "network.h"

namespace {

using namespace mincut;

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& message) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::cerr << "FAIL: " << message << '\n';
  }
}

Network makeNet(std::vector<std::string> names, int s, int t,
                std::vector<std::tuple<int, int, long long, std::string>> es) {
  Network net;
  net.node_names = std::move(names);
  net.source = s;
  net.sink = t;
  int i = 0;
  for (auto& [u, v, c, id] : es) {
    net.edges.push_back(InputEdge{id.empty() ? "e" + std::to_string(i) : id, u, v, c});
    ++i;
  }
  return net;
}

// Directly checks flow feasibility on a solve result (independent of the
// JSON verifier used by the integration tests).
void checkFlowFeasible(const Network& net, const SolveResult& r) {
  const int n = static_cast<int>(net.node_names.size());
  std::vector<long long> balance(n, 0);
  for (int i = 0; i < static_cast<int>(net.edges.size()); ++i) {
    const InputEdge& e = net.edges[i];
    const ArcLocation& loc = r.forward_arc[i];
    const ResidualArc& fwd = r.residual[loc.node][loc.index];
    const ResidualArc& rev = r.residual[fwd.to][fwd.rev];
    long long flow = e.capacity - fwd.cap;
    check(flow >= 0, "flow non-negative on " + e.id);
    check(flow <= e.capacity, "flow within capacity on " + e.id);
    check(rev.cap == flow, "artificial reverse residual cap equals flow on " + e.id);
    check(rev.artificial, "paired reverse arc flagged artificial on " + e.id);
    check(!fwd.artificial, "forward arc not artificial on " + e.id);
    balance[e.from] += flow;
    balance[e.to] -= flow;
  }
  for (int v = 0; v < n; ++v) {
    if (v != net.source && v != net.sink)
      check(balance[v] == 0, "conservation at node " + net.node_names[v]);
  }
  check(balance[net.source] == r.max_flow, "source balance equals max flow");
  check(-balance[net.sink] == r.max_flow, "sink balance equals max flow");

  // Cut edges are saturated and reachability matches no-residual-leaving-S.
  long long cut_sum = 0;
  for (int i = 0; i < static_cast<int>(net.edges.size()); ++i) {
    const InputEdge& e = net.edges[i];
    if (r.source_reachable[e.from] && !r.source_reachable[e.to]) {
      cut_sum += e.capacity;
      const ArcLocation& loc = r.forward_arc[i];
      const ResidualArc& fwd = r.residual[loc.node][loc.index];
      check(fwd.cap == 0, "cut edge " + e.id + " is saturated");
    }
  }
  check(cut_sum == r.max_flow, "reachable cut capacity equals max flow");
}

void testJsonRoundTrip() {
  std::string doc = R"({"nodes":["a","b"],"n":-3,"x":[1,true,false,null,"s"],
                        "f":1.5,"nest":{"k":"v\"q"}})";
  minjson::ParseError err;
  auto v = minjson::parse(doc, &err);
  check(v.has_value(), "JSON parses");
  if (!v) return;
  check(v->find("nodes")->items.size() == 2, "array parsed");
  check(v->find("n")->integer == -3, "negative integer parsed");
  check(v->find("nest")->find("k")->text == "v\"q", "escaped string parsed");
  std::string dumped = minjson::dump(*v, 0);
  auto v2 = minjson::parse(dumped, &err);
  check(v2.has_value(), "dumped JSON re-parses");
  check(minjson::dump(*v) == minjson::dump(*v2), "round trip stable");

  check(!minjson::parse("{bad}"), "malformed JSON rejected");
  check(!minjson::parse("[1,2,]"), "trailing comma rejected");
  check(!minjson::parse("\"unterminated"), "unterminated string rejected");
}

void testClassicNetwork() {
  // Classic CLRS s-t network: max flow = 23.
  auto net = makeNet({"s", "v1", "v2", "v3", "v4", "t"}, 0, 5, {
      {0, 1, 16, ""}, {0, 2, 13, ""}, {1, 2, 10, ""},
      {2, 1, 4, ""},  {1, 3, 12, ""}, {3, 2, 9, ""},
      {2, 4, 14, ""}, {4, 3, 7, ""},  {3, 5, 20, ""},
      {4, 5, 4, ""},
  });
  SolveResult r = Dinic(net).run();
  BruteResult b = bruteForceMinCut(net);
  check(r.max_flow == 23, "CLRS network max flow is 23");
  check(b.min_cut_value == 23, "brute force finds 23");
  check(r.max_flow == b.min_cut_value, "Dinic equals brute on CLRS network");
  checkFlowFeasible(net, r);
}

void testParallelEdges() {
  // Two parallel s->t edges: flows must be kept on separate arcs.
  auto net = makeNet({"s", "t"}, 0, 1, {
      {0, 1, 5, "p1"}, {0, 1, 7, "p2"},
  });
  SolveResult r = Dinic(net).run();
  check(r.max_flow == 12, "parallel edge capacities add");
  check(r.residual[0].size() == 2, "each parallel edge owns its own forward arc at s");
  check(r.residual[1].size() == 2, "each parallel edge owns its own artificial reverse arc at t");
  long long p1 = net.edges[0].capacity -
                 r.residual[r.forward_arc[0].node][r.forward_arc[0].index].cap;
  long long p2 = net.edges[1].capacity -
                 r.residual[r.forward_arc[1].node][r.forward_arc[1].index].cap;
  check(p1 == 5 && p2 == 7, "each parallel edge saturated independently");
  checkFlowFeasible(net, r);
  BruteResult b = bruteForceMinCut(net);
  check(b.min_cut_value == 12, "brute handles parallel edges");
}

void testZeroCapacity() {
  auto net = makeNet({"s", "a", "t"}, 0, 2, {
      {0, 1, 0, "z"}, {1, 2, 9, "ok"}, {0, 2, 3, "dir"},
  });
  SolveResult r = Dinic(net).run();
  check(r.max_flow == 3, "zero-capacity edge carries no flow");
  check(!r.source_reachable[1], "zero-capacity edge does not reach node a");
  checkFlowFeasible(net, r);
  BruteResult b = bruteForceMinCut(net);
  check(b.min_cut_value == 3, "brute with zero capacity = 3");
  check(b.partitions_checked == 2, "brute checks 2^(n-2) partitions");
}

void testSelfLoop() {
  auto net = makeNet({"s", "a", "t"}, 0, 2, {
      {0, 1, 5, "e1"}, {1, 1, 100, "loop"}, {1, 2, 5, "e2"},
  });
  SolveResult r = Dinic(net).run();
  check(r.max_flow == 5, "self loop does not affect max flow");
  checkFlowFeasible(net, r);
  BruteResult b = bruteForceMinCut(net);
  check(b.min_cut_value == 5, "brute ignores self loops");
}

void testDisconnected() {
  auto net = makeNet({"s", "iso", "t"}, 0, 2, {
      {0, 1, 10, "dead"},
  });
  SolveResult r = Dinic(net).run();
  check(r.max_flow == 0, "disconnected sink gives max flow 0");
  check(r.source_reachable[0] && r.source_reachable[1] &&
            !r.source_reachable[2],
        "reachability correct with no s-t path");
  checkFlowFeasible(net, r);
  BruteResult b = bruteForceMinCut(net);
  check(b.min_cut_value == 0, "brute disconnected cut value 0");
}

void testOppositeEdges() {
  // Edges in both directions between the same node pair get separate
  // residual pairs and must never cancel.
  auto net = makeNet({"s", "a", "t"}, 0, 2, {
      {0, 1, 8, "f"}, {1, 0, 3, "g"}, {1, 2, 6, "h"},
  });
  SolveResult r = Dinic(net).run();
  check(r.max_flow == 6, "opposite edges handled (flow 6)");
  check(r.residual[0].size() == 2, "s adjacency: f forward + g's artificial reverse");
  check(r.residual[1].size() == 3, "a adjacency: f's artificial reverse + g forward + h forward");
  checkFlowFeasible(net, r);
  BruteResult b = bruteForceMinCut(net);
  check(b.min_cut_value == 6, "brute opposite edges = 6");
}

// Randomized differential test: Dinic max flow must equal brute min cut on
// every generated small multigraph.
void testRandomDifferential(std::uint32_t seed, int trials) {
  std::mt19937 rng(seed);
  std::uniform_int_distribution<int> node_dist(2, 9);
  std::uniform_int_distribution<int> cap_dist(0, 9);  // includes 0
  std::uniform_int_distribution<int> extra(0, 4);
  for (int trial = 0; trial < trials; ++trial) {
    int n = node_dist(rng);
    int s = 0;
    int t = n - 1;
    std::vector<std::string> names;
    for (int v = 0; v < n; ++v) names.push_back("n" + std::to_string(v));
    Network net;
    net.node_names = names;
    net.source = s;
    net.sink = t;
    int edge_count = (n - 1) + extra(rng) * (n - 1) / 2 + 1;
    int id = 0;
    for (int k = 0; k < edge_count; ++k) {
      int u = static_cast<int>(rng() % static_cast<unsigned>(n));
      int v = static_cast<int>(rng() % static_cast<unsigned>(n));
      long long c = cap_dist(rng);
      net.edges.push_back(InputEdge{"e" + std::to_string(id++), u, v, c});
    }
    SolveResult r = Dinic(net).run();
    BruteResult b = bruteForceMinCut(net);
    if (r.max_flow != b.min_cut_value) {
      check(false, "random trial " + std::to_string(trial) +
                       " seed " + std::to_string(seed) + ": flow " +
                       std::to_string(r.max_flow) + " != cut " +
                       std::to_string(b.min_cut_value));
    }
    checkFlowFeasible(net, r);

    // The reachability cut must itself have capacity equal to the flow.
    long long cap = 0;
    for (const InputEdge& e : net.edges)
      if (r.source_reachable[e.from] && !r.source_reachable[e.to]) cap += e.capacity;
    check(cap == r.max_flow, "random trial reachability cut capacity equals flow");
  }
  check(true, "random differential trials completed");
}

}  // namespace

int main() {
  testJsonRoundTrip();
  testClassicNetwork();
  testParallelEdges();
  testZeroCapacity();
  testSelfLoop();
  testDisconnected();
  testOppositeEdges();
  testRandomDifferential(/*seed=*/20260925u, /*trials=*/300);

  std::cout << "C++ unit tests: " << g_checks - g_failures << "/" << g_checks
            << " checks passed\n";
  if (g_failures) {
    std::cerr << g_failures << " FAILURES\n";
    return 1;
  }
  return 0;
}
