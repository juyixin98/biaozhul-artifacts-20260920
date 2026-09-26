// Unit tests for the core modules. Built as a standalone binary; no test
// framework dependency — a tiny assertion harness is enough. Covers the
// algorithm core and, importantly, negative tests proving the verifier
// rejects tampered (invalid) flows and partitions.

#include <cstdint>
#include <cstdio>
#include <functional>
#include <string>
#include <vector>

#include "bruteforce.hpp"
#include "graph.hpp"
#include "maxflow.hpp"
#include "mincut.hpp"
#include "verifier.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& name) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::printf("  FAIL: %s\n", name.c_str());
  }
}

void check_eq(std::int64_t got, std::int64_t want, const std::string& name) {
  ++g_checks;
  if (got != want) {
    ++g_failures;
    std::printf("  FAIL: %s (got %lld, want %lld)\n", name.c_str(),
                static_cast<long long>(got), static_cast<long long>(want));
  }
}

mcut::Problem make_problem(int n, int s, int t,
                           std::vector<mcut::InputEdge> edges) {
  mcut::Problem p;
  p.num_vertices = n;
  p.source = s;
  p.sink = t;
  p.edges = std::move(edges);
  return p;
}

mcut::InputEdge edge(const std::string& id, int from, int to,
                     std::int64_t cap) {
  mcut::InputEdge e;
  e.id = id;
  e.from = from;
  e.to = to;
  e.capacity = cap;
  return e;
}

// Runs Dinic, builds the cut and fully verifies it. Returns the report so a
// test can additionally inspect the flow value.
mcut::VerifyReport solve_and_verify(const mcut::Problem& p,
                                    std::int64_t& flow_value_out,
                                    mcut::CutCertificate& cut_out) {
  mcut::Dinic dinic(p.num_vertices);
  for (int i = 0; i < static_cast<int>(p.edges.size()); ++i) {
    dinic.add_original_edge(i, p.edges[i].from, p.edges[i].to,
                            p.edges[i].capacity);
  }
  flow_value_out = dinic.compute_max_flow(p.source, p.sink);
  std::vector<std::int64_t> flows(p.edges.size());
  for (int i = 0; i < static_cast<int>(p.edges.size()); ++i) {
    flows[i] = dinic.edge_flow(i);
  }
  cut_out = mcut::build_min_cut(p, dinic, p.source);
  return mcut::verify_solution(p, flows, cut_out.source_side, flow_value_out);
}

void test_basic_diamond() {
  // s=0, t=3; two paths 0-1-3 and 0-2-3 with capacities (3,2) and (2,3).
  auto p = make_problem(4, 0, 3,
                        {edge("a", 0, 1, 3), edge("b", 0, 2, 2),
                         edge("d", 1, 3, 2), edge("e", 2, 3, 3)});
  std::int64_t fv;
  mcut::CutCertificate cut;
  auto rep = solve_and_verify(p, fv, cut);
  check(rep.ok, "diamond: all verification checks pass");
  check_eq(fv, 4, "diamond: max flow value");
  check_eq(cut.cut_value, 4, "diamond: cut value");
  auto bf = mcut::brute_force_min_cut(p);
  check_eq(bf.min_cut_value, 4, "diamond: brute force agrees");
}

void test_parallel_edges() {
  // Two parallel edges 0->1 (4 and 3) then 1->2 cap 5: flow = 5.
  auto p = make_problem(3, 0, 2,
                        {edge("p1", 0, 1, 4), edge("p2", 0, 1, 3),
                         edge("e", 1, 2, 5)});
  std::int64_t fv;
  mcut::CutCertificate cut;
  auto rep = solve_and_verify(p, fv, cut);
  check(rep.ok, "parallel: verifies");
  check_eq(fv, 5, "parallel: bottleneck is edge 1->2 = 5 (both parallel "
                  "edges combined do not exceed it)");
  // Both parallel edges must carry flow, and their flows sum to the total.
  mcut::Dinic dinic(p.num_vertices);
  for (int i = 0; i < static_cast<int>(p.edges.size()); ++i) {
    dinic.add_original_edge(i, p.edges[i].from, p.edges[i].to,
                            p.edges[i].capacity);
  }
  dinic.compute_max_flow(p.source, p.sink);
  check_eq(dinic.edge_flow(0) + dinic.edge_flow(1), 5,
           "parallel: flow split across both parallel edges sums to 5");
  check(dinic.edge_flow(0) > 0 && dinic.edge_flow(1) > 0,
        "parallel: each parallel edge carries positive flow");
}

void test_zero_capacity() {
  // Zero-capacity edges must be accepted, carry no flow, and may appear as a
  // saturated cut edge (flow 0 == capacity 0).
  auto p = make_problem(3, 0, 2,
                        {edge("z", 0, 1, 0), edge("e", 1, 2, 5),
                         edge("direct", 0, 2, 2)});
  std::int64_t fv;
  mcut::CutCertificate cut;
  auto rep = solve_and_verify(p, fv, cut);
  check(rep.ok, "zero-cap: verifies");
  check_eq(fv, 2, "zero-cap: only direct edge carries flow");
  bool found_zero_saturated = false;
  for (const auto& ce : cut.cut_edges) {
    if (p.edges[ce.edge_id].id == "direct" && ce.flow == 2) {
      found_zero_saturated = true;
    }
  }
  check(found_zero_saturated, "zero-cap: direct edge is the cut edge");
}

void test_original_reverse_edge_separate() {
  // The requirement: a residual reverse arc must be kept separate from an
  // original edge in the reverse direction. Graph:
  //   0->1 cap 5, 1->0 cap 3 (original reverse edge), 1->2 cap 5.
  // Max flow 0->2 must be 5 and the 1->0 original edge must carry 0 (there
  // is no reason to push flow backward); its capacity 3 must never be added
  // to residual capacity of the 0->1 edge.
  auto p = make_problem(3, 0, 2,
                        {edge("fwd", 0, 1, 5), edge("orig_rev", 1, 0, 3),
                         edge("out", 1, 2, 5)});
  mcut::Dinic dinic(3);
  dinic.add_original_edge(0, 0, 1, 5);
  dinic.add_original_edge(1, 1, 0, 3);
  dinic.add_original_edge(2, 1, 2, 5);
  std::int64_t fv = dinic.compute_max_flow(0, 2);
  check_eq(fv, 5, "reverse-separate: flow is 5");
  check_eq(dinic.edge_flow(0), 5, "reverse-separate: 0->1 saturated");
  check_eq(dinic.edge_flow(1), 0,
           "reverse-separate: original 1->0 edge carries 0 (never merged "
           "with residual arc)");
  check_eq(dinic.edge_flow(2), 5, "reverse-separate: 1->2 saturated");
}

void test_unreachable_sink() {
  auto p = make_problem(3, 0, 2, {edge("x", 0, 1, 5)});
  std::int64_t fv;
  mcut::CutCertificate cut;
  auto rep = solve_and_verify(p, fv, cut);
  check(rep.ok, "unreachable: verifies");
  check_eq(fv, 0, "unreachable: zero flow");
  check_eq(cut.cut_value, 0, "unreachable: empty cut value 0");
  check(cut.cut_edges.empty(), "unreachable: no cut edges");
  auto bf = mcut::brute_force_min_cut(p);
  check_eq(bf.min_cut_value, 0, "unreachable: brute force 0");
}

void test_all_zero_capacities() {
  auto p = make_problem(3, 0, 2,
                        {edge("a", 0, 1, 0), edge("b", 1, 2, 0)});
  std::int64_t fv;
  mcut::CutCertificate cut;
  auto rep = solve_and_verify(p, fv, cut);
  check(rep.ok, "all-zero: verifies");
  check_eq(fv, 0, "all-zero: zero flow");
  auto bf = mcut::brute_force_min_cut(p);
  check_eq(bf.min_cut_value, 0, "all-zero: brute force 0");
}

// ---- Negative tests: the verifier must reject invalid certificates ----

void test_verifier_rejects_capacity_violation() {
  auto p = make_problem(2, 0, 1, {edge("a", 0, 1, 3)});
  std::vector<std::int64_t> bad_flows = {4};  // 4 > capacity 3
  std::vector<unsigned char> side = {1, 0};
  auto rep = mcut::verify_solution(p, bad_flows, side, 4);
  check(!rep.ok, "bad flow over capacity rejected");
}

void test_verifier_rejects_negative_flow() {
  auto p = make_problem(2, 0, 1, {edge("a", 0, 1, 3)});
  std::vector<std::int64_t> bad_flows = {-1};
  std::vector<unsigned char> side = {1, 0};
  auto rep = mcut::verify_solution(p, bad_flows, side, -1);
  check(!rep.ok, "negative flow rejected");
}

void test_verifier_rejects_conservation_violation() {
  // Path 0->1->2, tamper flows so middle vertex loses one unit.
  auto p = make_problem(3, 0, 2,
                        {edge("a", 0, 1, 5), edge("b", 1, 2, 5)});
  std::vector<std::int64_t> bad_flows = {5, 4};
  std::vector<unsigned char> side = {1, 1, 0};
  auto rep = mcut::verify_solution(p, bad_flows, side, 5);
  check(!rep.ok, "flow conservation violation rejected");
}

void test_verifier_rejects_wrong_partition() {
  // Valid flow of value 5, but claim the sink is on the source side.
  auto p = make_problem(2, 0, 1, {edge("a", 0, 1, 5)});
  std::vector<std::int64_t> flows = {5};
  std::vector<unsigned char> bogus_side = {1, 1};  // t wrongly in S
  auto rep = mcut::verify_solution(p, flows, bogus_side, 5);
  check(!rep.ok, "partition containing sink rejected");
}

void test_verifier_rejects_value_mismatch() {
  auto p = make_problem(2, 0, 1, {edge("a", 0, 1, 5)});
  std::vector<std::int64_t> flows = {5};
  std::vector<unsigned char> side = {1, 0};
  auto rep = mcut::verify_solution(p, flows, side, 6);  // lie about value
  check(!rep.ok, "claimed value mismatch rejected");
}

void test_self_loop_reports_zero_flow() {
  // A self loop can never be augmented (level[v] == level[u]+1 is impossible
  // when u == v). Its reported flow must therefore be exactly 0, and the
  // forward/reverse arcs must be correctly paired even though both land in
  // the same adjacency list.
  auto p = make_problem(3, 0, 2,
                        {edge("loop", 1, 1, 99), edge("a", 0, 1, 4),
                         edge("b", 1, 2, 4)});
  mcut::Dinic dinic(3);
  dinic.add_original_edge(0, 1, 1, 99);  // self loop, edge_id 0
  dinic.add_original_edge(1, 0, 1, 4);
  dinic.add_original_edge(2, 1, 2, 4);
  std::int64_t fv = dinic.compute_max_flow(0, 2);
  check_eq(fv, 4, "self-loop: max flow unaffected");
  check_eq(dinic.edge_flow(0), 0,
           "self-loop: reported flow is 0 (forward arc, not its reverse)");
  check_eq(dinic.edge_capacity(0), 99, "self-loop: capacity preserved");
}

void test_bruteforce_enumeration_count() {
  auto p = make_problem(5, 0, 4, {});
  auto bf = mcut::brute_force_min_cut(p);
  check_eq(static_cast<std::int64_t>(bf.partitions_checked), 8,
           "brute force checks 2^(n-2) partitions");
}

}  // namespace

int main() {
  struct Case {
    std::string name;
    std::function<void()> fn;
  };
  const std::vector<Case> cases = {
      {"basic diamond", test_basic_diamond},
      {"parallel edges", test_parallel_edges},
      {"zero capacity", test_zero_capacity},
      {"original reverse edge kept separate",
       test_original_reverse_edge_separate},
      {"unreachable sink", test_unreachable_sink},
      {"all zero capacities", test_all_zero_capacities},
      {"self loop reports zero flow", test_self_loop_reports_zero_flow},
      {"reject capacity violation", test_verifier_rejects_capacity_violation},
      {"reject negative flow", test_verifier_rejects_negative_flow},
      {"reject conservation violation",
       test_verifier_rejects_conservation_violation},
      {"reject wrong partition", test_verifier_rejects_wrong_partition},
      {"reject value mismatch", test_verifier_rejects_value_mismatch},
      {"bruteforce enumeration count", test_bruteforce_enumeration_count},
  };

  for (const auto& c : cases) {
    std::printf("[ RUN  ] %s\n", c.name.c_str());
    c.fn();
  }

  std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
