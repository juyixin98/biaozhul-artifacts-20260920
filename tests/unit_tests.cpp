// Unit tests for the dominator backend.
//
// Covers: JSON round-trip, graph validation, reachability with an
// unreachable cycle, immediate dominators, dominance frontiers on
// diamonds/loops/multi-exit graphs, back-edge evidence, query layer,
// and a randomized cross-check of the production solver against the
// naive path-enumeration reference.
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>
#include <vector>

#include "api.hpp"
#include "dom.hpp"
#include "graph.hpp"
#include "json.hpp"
#include "naive.hpp"

namespace {

using domjson::JsonValue;
using domtree::Edge;
using domtree::Graph;
using domtree::NaiveResult;
using domtree::SolverResult;

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& what) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::cerr << "FAIL: " << what << "\n";
  }
}

Graph make_graph(
    const std::vector<std::string>& nodes,
    const std::vector<std::pair<std::string, std::string>>& edges) {
  std::string err;
  Graph g = Graph::build(nodes, edges, err);
  if (!err.empty()) {
    std::cerr << "FAIL: graph build error: " << err << "\n";
    ++g_failures;
  }
  return g;
}

SolverResult solve_or_fail(const Graph& g, const std::string& entry) {
  SolverResult r;
  std::string err;
  if (!domtree::solve(g, g.id(entry), r, err)) {
    std::cerr << "FAIL: solve error: " << err << "\n";
    ++g_failures;
  }
  return r;
}

bool frontier_has(const SolverResult& r, const Graph& g,
                  const std::string& v, const std::string& b) {
  for (int x : r.frontier[g.id(v)])
    if (g.name(x) == b) return true;
  return false;
}

int frontier_size(const SolverResult& r, const Graph& g,
                  const std::string& v) {
  return static_cast<int>(r.frontier[g.id(v)].size());
}

// ---------------------------------------------------------------- JSON

void test_json_roundtrip() {
  std::string err;
  JsonValue v;
  check(domjson::parse_json(
            R"({"a":[1,2.5,"x\ny"],"b":{"c":true,"d":null},"e":"é"})",
            v, err),
        "json parse of mixed document");
  check(v.is_object(), "json root is object");
  const JsonValue* a = v.find("a");
  check(a && a->is_array() && a->arr.size() == 3, "json array shape");
  check(a && a->arr[2].str == "x\ny", "json string escape decode");
  const JsonValue* b = v.find("b");
  check(b && b->find("c") && b->find("c")->boolean, "json nested bool");
  check(b && b->find("d") && b->find("d")->is_null(), "json null");

  std::string dumped = domjson::dump_json(v);
  JsonValue v2;
  check(domjson::parse_json(dumped, v2, err), "json re-parse of dump");
  check(v2.find("a") && v2.find("a")->arr[2].str == "x\ny",
        "json round-trip preserves string");

  JsonValue bad;
  check(!domjson::parse_json("{bad", bad, err), "json rejects garbage");
  check(domjson::parse_json("[1,2]", bad, err), "json array parses");
  check(!domjson::parse_json("{\"a\":1} trailing", bad, err),
        "json rejects trailing garbage");
  check(!domjson::parse_json("01", bad, err), "json rejects leading zero");
}

// ---------------------------------------------------------------- graph

void test_graph_validation() {
  std::string err;
  Graph g1 = Graph::build({}, {}, err);
  check(!err.empty() && g1.node_count() == 0, "empty node list rejected");

  err.clear();
  Graph g2 = Graph::build({"A", "A"}, {}, err);
  check(!err.empty(), "duplicate labels rejected");

  err.clear();
  Graph g3 = Graph::build({"A"}, {{"A", "B"}}, err);
  check(!err.empty(), "edge to unknown node rejected");

  err.clear();
  Graph g4 = Graph::build({"A", "B"}, {{"A", "B"}, {"A", "B"}}, err);
  check(err.empty() && g4.edge_count() == 1, "parallel edges deduplicated");

  err.clear();
  std::vector<std::string> many(domtree::kMaxNodes + 1, "n");
  for (size_t i = 0; i < many.size(); ++i) many[i] = "n" + std::to_string(i);
  Graph g5 = Graph::build(many, {}, err);
  check(!err.empty(), "node limit enforced");
}

// ------------------------------------------------------- solver basics

// Chain: A -> B -> C, plus unreachable self-loop X -> X and edge X -> B.
void test_chain_with_unreachable() {
  Graph g = make_graph({"A", "B", "C", "X"},
                       {{"A", "B"}, {"B", "C"}, {"X", "X"}, {"X", "B"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.reachable_count == 3, "chain: 3 reachable of 4");
  check(!r.reachable[g.id("X")], "chain: X unreachable");
  check(r.idom[g.id("A")] == -1, "chain: entry has no idom");
  check(r.idom[g.id("B")] == g.id("A"), "chain: idom(B)=A");
  check(r.idom[g.id("C")] == g.id("B"), "chain: idom(C)=B");
  check(r.idom[g.id("X")] == -1, "chain: unreachable X has no idom");
  check(r.dominates(g.id("A"), g.id("C")), "chain: A dominates C");
  check(!r.dominates(g.id("C"), g.id("A")), "chain: C !dom A");
  check(!r.dominates(g.id("X"), g.id("X")),
        "chain: unreachable node does not dominate itself");
  check(frontier_size(r, g, "A") == 0 && frontier_size(r, g, "B") == 0 &&
            frontier_size(r, g, "C") == 0,
        "chain: all frontiers empty");
  check(r.back_edges.empty(), "chain: no reachable back edges");
  // X->X and X->B touch unreachable endpoints.
  check(r.unreachable_edges.size() == 2,
        "chain: 2 edges touching unreachable nodes");
}

// Diamond: A -> {B, C} -> D. Classic join; DF(B)=DF(C)={D}.
void test_diamond() {
  Graph g = make_graph({"A", "B", "C", "D"},
                       {{"A", "B"}, {"A", "C"}, {"B", "D"}, {"C", "D"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.idom[g.id("D")] == g.id("A"), "diamond: idom(D)=A");
  check(frontier_has(r, g, "B", "D") && frontier_size(r, g, "B") == 1,
        "diamond: DF(B)={D}");
  check(frontier_has(r, g, "C", "D") && frontier_size(r, g, "C") == 1,
        "diamond: DF(C)={D}");
  check(frontier_size(r, g, "A") == 0 && frontier_size(r, g, "D") == 0,
        "diamond: DF(A)=DF(D)=empty");
}

// Loop: A -> B, B -> {C, D}, C -> B (back edge), D -> E (exit).
// idom: B<-A, C<-B, D<-B, E<-D. DF(B)={B}, DF(C)={B}.
void test_loop_back_edge() {
  Graph g = make_graph({"A", "B", "C", "D", "E"},
                       {{"A", "B"},
                        {"B", "C"},
                        {"B", "D"},
                        {"C", "B"},
                        {"D", "E"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.idom[g.id("B")] == g.id("A"), "loop: idom(B)=A");
  check(r.idom[g.id("C")] == g.id("B"), "loop: idom(C)=B");
  check(r.idom[g.id("E")] == g.id("D"), "loop: idom(E)=D");
  check(frontier_has(r, g, "B", "B") && frontier_size(r, g, "B") == 1,
        "loop: DF(B)={B} (loop header in own frontier)");
  check(frontier_has(r, g, "C", "B") && frontier_size(r, g, "C") == 1,
        "loop: DF(C)={B}");
  check(r.back_edges.size() == 1 &&
            g.name(r.back_edges[0].from) == "C" &&
            g.name(r.back_edges[0].to) == "B",
        "loop: back edge C->B detected");
}

// Multi-exit: A -> B; B -> C, B -> D, B -> E (three exits from B);
// C -> F; D -> F; E -> F. F has 3 preds. B strictly dominates F, so
// DF(B) is empty; the side blocks each have DF={F}.
void test_multi_exit() {
  Graph g = make_graph({"A", "B", "C", "D", "E", "F"},
                       {{"A", "B"},
                        {"B", "C"},
                        {"B", "D"},
                        {"B", "E"},
                        {"C", "F"},
                        {"D", "F"},
                        {"E", "F"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.idom[g.id("F")] == g.id("B"), "multi-exit: idom(F)=B");
  check(frontier_size(r, g, "B") == 0,
        "multi-exit: DF(B) empty (B strictly dominates F)");
  for (const char* v : {"C", "D", "E"}) {
    check(frontier_has(r, g, v, "F") && frontier_size(r, g, v) == 1,
          std::string("multi-exit: DF(") + v + ")={F}");
  }
  check(r.back_edges.empty(), "multi-exit: no back edges");
}

// Unreachable cycle: A -> B; cycle P <-> Q disconnected from entry.
void test_unreachable_cycle() {
  Graph g = make_graph({"A", "B", "P", "Q"},
                       {{"A", "B"}, {"P", "Q"}, {"Q", "P"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.reachable_count == 2, "unreachable cycle: only A,B reachable");
  check(!r.reachable[g.id("P")] && !r.reachable[g.id("Q")],
        "unreachable cycle: P,Q unreachable");
  check(r.idom[g.id("P")] == -1 && r.idom[g.id("Q")] == -1,
        "unreachable cycle: no idom for P,Q");
  check(r.frontier[g.id("P")].empty() && r.frontier[g.id("Q")].empty(),
        "unreachable cycle: empty frontiers for P,Q");
  check(r.back_edges.empty(),
        "unreachable cycle: cycle edges are not reported as back edges");
  check(r.unreachable_edges.size() == 2,
        "unreachable cycle: P->Q and Q->P reported as unreachable edges");
}

// Entry self-loop: A -> A, A -> B. By the formal definition A is in its
// own frontier (A dominates pred A of A, but not strictly dominates A).
void test_entry_self_loop() {
  Graph g = make_graph({"A", "B"}, {{"A", "A"}, {"A", "B"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.idom[g.id("B")] == g.id("A"), "self-loop: idom(B)=A");
  check(frontier_has(r, g, "A", "A"), "self-loop: A in DF(A)");
  check(r.back_edges.size() == 1, "self-loop: A->A is a back edge");
}

// Nested loops: A->B (outer header), B->C (inner header), C->D,
// D->C (inner back edge), D->E, E->B (outer back edge).
void test_nested_loop() {
  Graph g = make_graph({"A", "B", "C", "D", "E"},
                       {{"A", "B"},
                        {"B", "C"},
                        {"C", "D"},
                        {"D", "C"},
                        {"D", "E"},
                        {"E", "B"}});
  SolverResult r = solve_or_fail(g, "A");
  check(r.idom[g.id("B")] == g.id("A"), "nested: idom(B)=A");
  check(r.idom[g.id("C")] == g.id("B"), "nested: idom(C)=B");
  check(r.idom[g.id("D")] == g.id("C"), "nested: idom(D)=C");
  check(r.idom[g.id("E")] == g.id("D"), "nested: idom(E)=D");
  check(frontier_has(r, g, "B", "B"), "nested: B in DF(B) (outer header)");
  check(frontier_has(r, g, "C", "C"), "nested: C in DF(C) (inner header)");
  check(frontier_has(r, g, "C", "B"), "nested: B in DF(C) (outer join)");
  check(frontier_has(r, g, "D", "C") && frontier_has(r, g, "D", "B"),
        "nested: DF(D) contains C and B");
  check(frontier_has(r, g, "E", "B"), "nested: B in DF(E)");
  check(r.back_edges.size() == 2, "nested: two back edges detected");
}

// ------------------------------------------------- naive cross-check

void cross_check(const Graph& g, const std::string& entry,
                 const std::string& tag) {
  SolverResult prod;
  std::string err;
  if (!domtree::solve(g, g.id(entry), prod, err)) {
    check(false, tag + ": production solve failed: " + err);
    return;
  }
  NaiveResult ref;
  if (!domtree::naive_solve(g, g.id(entry), ref, err)) {
    check(false, tag + ": naive solve failed: " + err);
    return;
  }
  std::vector<std::string> mm = domtree::compare_results(g, prod, ref);
  for (const std::string& m : mm) {
    check(false, tag + ": " + m);
  }
}

void test_fixed_cross_checks() {
  cross_check(make_graph({"A", "B", "C", "D"},
                         {{"A", "B"}, {"A", "C"}, {"B", "D"}, {"C", "D"}}),
              "A", "cross diamond");
  cross_check(make_graph({"A", "B", "C", "D", "E"},
                         {{"A", "B"},
                          {"B", "C"},
                          {"B", "D"},
                          {"C", "B"},
                          {"D", "E"}}),
              "A", "cross loop");
  cross_check(make_graph({"A", "B"}, {{"A", "A"}, {"A", "B"}}), "A",
              "cross self-loop");
  cross_check(make_graph({"A", "B", "P", "Q"},
                         {{"A", "B"}, {"P", "Q"}, {"Q", "P"}}),
              "A", "cross unreachable cycle");
  // If-conversion style: A->{B,C}, B->D, C->D, D->{E,F}, E->G, F->G.
  cross_check(make_graph({"A", "B", "C", "D", "E", "F", "G"},
                         {{"A", "B"},
                          {"A", "C"},
                          {"B", "D"},
                          {"C", "D"},
                          {"D", "E"},
                          {"D", "F"},
                          {"E", "G"},
                          {"F", "G"}}),
              "A", "cross two-level diamond");
}

void test_random_cross_checks() {
  std::mt19937 rng(20260922);
  int graphs = 0;
  for (int trial = 0; trial < 400; ++trial) {
    int n = 2 + static_cast<int>(rng() % 11);  // 2..12 nodes
    std::vector<std::string> nodes;
    for (int i = 0; i < n; ++i) nodes.push_back("n" + std::to_string(i));
    std::vector<std::pair<std::string, std::string>> edges;
    // Random edges with ~25% density, self loops allowed.
    for (int i = 0; i < n; ++i) {
      for (int j = 0; j < n; ++j) {
        if (rng() % 4 == 0) edges.emplace_back(nodes[i], nodes[j]);
      }
    }
    Graph g = make_graph(nodes, edges);
    cross_check(g, "n0", "random graph #" + std::to_string(trial));
    ++graphs;
  }
  std::cout << "random cross-check: " << graphs << " graphs compared\n";
}

// ----------------------------------------------------------------- api

std::string run_api(const std::string& req) {
  return domtree::handle_request(req);
}

JsonValue parse_or_fail(const std::string& text, const std::string& tag) {
  JsonValue v;
  std::string err;
  if (!domjson::parse_json(text, v, err)) {
    check(false, tag + ": response is not valid JSON: " + err);
  }
  return v;
}

void test_api_analyze_and_queries() {
  std::string resp = run_api(R"({
    "entry": "A",
    "nodes": ["A","B","C","D","E","X","Y"],
    "edges": [["A","B"],["B","C"],["B","D"],["C","B"],["D","E"],
              ["X","Y"],["Y","X"]],
    "queries": [
      {"type":"dominates","a":"B","b":"C"},
      {"type":"dominates","a":"C","b":"B"},
      {"type":"strictly_dominates","a":"B","b":"B"},
      {"type":"idom","node":"C"},
      {"type":"idom","node":"A"},
      {"type":"frontier","node":"C"},
      {"type":"dominators","node":"E"},
      {"type":"dominated_by","node":"B"},
      {"type":"dom_chain","node":"E"},
      {"type":"idom","node":"X"},
      {"type":"dominates","a":"X","b":"B"},
      {"type":"idom","node":"ZZZ"},
      {"type":"bogus","node":"A"}
    ],
    "verify_naive": true
  })");
  JsonValue v = parse_or_fail(resp, "api analyze");
  check(v.find("success") && v.find("success")->boolean, "api: success");
  const JsonValue* result = v.find("result");
  check(result != nullptr, "api: result present");
  const JsonValue* unr = result ? result->find("unreachable") : nullptr;
  check(unr && unr->is_array() && unr->arr.size() == 2,
        "api: 2 unreachable nodes reported");
  const JsonValue* be = result ? result->find("evidence") : nullptr;
  check(be && be->find("back_edges") &&
            be->find("back_edges")->arr.size() == 1,
        "api: one back edge in evidence");
  const JsonValue* stats = result ? result->find("statistics") : nullptr;
  check(stats && stats->find("dataflow_iterations") &&
            stats->find("dataflow_iterations")->number >= 1,
        "api: statistics include iteration count");

  const JsonValue* qs = v.find("queries");
  check(qs && qs->arr.size() == 13, "api: 13 query answers");
  if (!qs || qs->arr.size() != 13) return;
  const auto& a = qs->arr;
  check(a[0].find("result") && a[0].find("result")->boolean,
        "api: B dominates C");
  check(a[1].find("result") && !a[1].find("result")->boolean,
        "api: C does not dominate B");
  check(a[2].find("result") && !a[2].find("result")->boolean,
        "api: B does not strictly dominate B");
  check(a[3].find("idom") && a[3].find("idom")->str == "B",
        "api: idom(C)=B");
  check(a[4].find("idom") && a[4].find("idom")->is_null(),
        "api: idom(A)=null");
  check(a[5].find("frontier") && a[5].find("frontier")->arr.size() == 1 &&
            a[5].find("frontier")->arr[0].str == "B",
        "api: DF(C)={B}");
  check(a[6].find("dominators") &&
            a[6].find("dominators")->arr.size() == 4,
        "api: dominators(E)={A,B,D,E}");
  check(a[7].find("subtree") && a[7].find("subtree")->arr.size() == 4,
        "api: subtree(B)={B,C,D,E}");
  check(a[8].find("chain") && a[8].find("chain")->arr.size() == 4,
        "api: chain(E)=[E,D,B,A]");
  check(a[9].find("ok") && !a[9].find("ok")->boolean,
        "api: idom of unreachable X is an error");
  check(a[10].find("ok") && !a[10].find("ok")->boolean,
        "api: dominates with unreachable operand is an error");
  check(a[11].find("ok") && !a[11].find("ok")->boolean,
        "api: unknown node rejected");
  check(a[12].find("ok") && !a[12].find("ok")->boolean,
        "api: unknown query type rejected");

  const JsonValue* nv = v.find("naive_verification");
  check(nv && nv->find("ran") && nv->find("ran")->boolean,
        "api: naive verification ran");
  check(nv && nv->find("match") && nv->find("match")->boolean,
        "api: naive verification matches");
}

void test_api_errors() {
  JsonValue v = parse_or_fail(run_api("not json"), "api garbage");
  check(v.find("success") && !v.find("success")->boolean,
        "api: garbage rejected");
  check(v.find("error") != nullptr, "api: error message present");

  v = parse_or_fail(run_api("{}"), "api empty");
  check(v.find("success") && !v.find("success")->boolean,
        "api: missing entry rejected");

  v = parse_or_fail(run_api(R"({"entry":"A","nodes":["A"],"edges":[]})"),
                    "api minimal");
  check(v.find("success") && v.find("success")->boolean,
        "api: single-node graph ok");

  v = parse_or_fail(
      run_api(R"({"entry":"Z","nodes":["A"],"edges":[]})"), "api bad entry");
  check(v.find("success") && !v.find("success")->boolean,
        "api: unknown entry rejected");

  v = parse_or_fail(
      run_api(R"({"entry":"A","nodes":["A"],"edges":[["A","B"]]})"),
      "api bad edge");
  check(v.find("success") && !v.find("success")->boolean,
        "api: edge to unknown node rejected");

  v = parse_or_fail(
      run_api(R"({"entry":"A","nodes":["A","A"],"edges":[]})"),
      "api dup node");
  check(v.find("success") && !v.find("success")->boolean,
        "api: duplicate node rejected");

  v = parse_or_fail(
      run_api(R"({"entry":"A","nodes":["A"],"edges":[{"from":"A"}]})"),
      "api malformed edge");
  check(v.find("success") && !v.find("success")->boolean,
        "api: malformed edge object rejected");

  // Object-style edges are accepted.
  v = parse_or_fail(
      run_api(
          R"({"entry":"A","nodes":["A","B"],"edges":[{"from":"A","to":"B"}]})"),
      "api object edges");
  check(v.find("success") && v.find("success")->boolean,
        "api: object-style edges accepted");
}

void test_api_scale_limit() {
  // kMaxNodes+1 distinct labels must be rejected through the API too.
  std::string req = R"({"entry":"n0","nodes":[)";
  for (int i = 0; i <= domtree::kMaxNodes; ++i) {
    if (i) req += ",";
    req += "\"n" + std::to_string(i) + "\"";
  }
  req += "]}";
  JsonValue v = parse_or_fail(run_api(req), "api scale");
  check(v.find("success") && !v.find("success")->boolean,
        "api: oversized graph rejected");
}

}  // namespace

int main() {
  test_json_roundtrip();
  test_graph_validation();
  test_chain_with_unreachable();
  test_diamond();
  test_loop_back_edge();
  test_multi_exit();
  test_unreachable_cycle();
  test_entry_self_loop();
  test_nested_loop();
  test_fixed_cross_checks();
  test_random_cross_checks();
  test_api_analyze_and_queries();
  test_api_errors();
  test_api_scale_limit();

  std::cout << (g_failures == 0 ? "ALL TESTS PASSED" : "TESTS FAILED")
            << " (" << (g_checks - g_failures) << "/" << g_checks
            << " checks)\n";
  return g_failures == 0 ? 0 : 1;
}
