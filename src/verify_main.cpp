// Independent verifier: checks a solver response against the original
// request. Validates the component partition, deterministic numbering, cycle
// witnesses, condensation edge set (exact, deduplicated), DAG acyclicity,
// and — for small graphs — component maximality via a reachability matrix.
//
// Usage: scc_verify <request.json> <result.json>
// Exit codes: 0 all checks passed, 1 verification failed, 2 bad input.

#include <algorithm>
#include <fstream>
#include <iostream>
#include <queue>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "json.hpp"
#include "scc.hpp"

namespace {

constexpr int kMaximalityCheckMaxVertices = 3000;

std::string readFile(const std::string& path) {
  std::ifstream in(path);
  if (!in) throw std::runtime_error("cannot open file: " + path);
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

[[noreturn]] void fail(const std::string& msg) {
  std::cout << "VERIFY FAIL: " << msg << "\n";
  std::exit(1);
}

void expect(bool cond, const std::string& msg) {
  if (!cond) fail(msg);
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::cerr << "usage: scc_verify <request.json> <result.json>\n";
    return 2;
  }

  scc::Graph g;
  scc::Json result;
  try {
    const scc::Json req = scc::Json::parse(readFile(argv[1]));
    g = scc::graphFromJson(req);
    result = scc::Json::parse(readFile(argv[2]));
  } catch (const std::exception& e) {
    std::cerr << "input error: " << e.what() << "\n";
    return 2;
  }

  const int n = g.vertexCount;

  // --- Response envelope -------------------------------------------------
  const scc::Json* ok = result.find("ok");
  expect(ok && ok->isBool() && ok->asBool(), "result.ok is not true");
  const scc::Json* compsJson = result.find("components");
  expect(compsJson && compsJson->isArray(), "components missing/not array");
  const scc::Json* condJson = result.find("condensation_edges");
  expect(condJson && condJson->isArray(),
         "condensation_edges missing/not array");

  const auto& comps = compsJson->asArray();
  const int k = static_cast<int>(comps.size());

  // --- Partition + deterministic numbering -------------------------------
  std::vector<int> v2c(static_cast<size_t>(n), -1);
  std::vector<std::vector<int>> compVerts(static_cast<size_t>(k));
  for (int i = 0; i < k; ++i) {
    const scc::Json& c = comps[static_cast<size_t>(i)];
    const scc::Json* id = c.find("id");
    expect(id && id->isInt() && id->asInt() == i,
           "component ids must be 0..k-1 in order");
    const scc::Json* verts = c.find("vertices");
    expect(verts && verts->isArray(), "component vertices missing/not array");
    int64_t prev = -1;
    for (const scc::Json& jv : verts->asArray()) {
      expect(jv.isInt(), "component vertex must be integer");
      const int64_t v = jv.asInt();
      expect(v >= 0 && v < n, "component vertex out of range");
      expect(v > prev, "component vertices must be sorted ascending");
      prev = v;
      expect(v2c[static_cast<size_t>(v)] == -1,
             "vertex appears in two components");
      v2c[static_cast<size_t>(v)] = i;
      compVerts[static_cast<size_t>(i)].push_back(static_cast<int>(v));
    }
    expect(!verts->asArray().empty(), "empty component");
  }
  for (int v = 0; v < n; ++v)
    expect(v2c[static_cast<size_t>(v)] != -1,
           "vertex " + std::to_string(v) + " not covered by any component");
  for (int i = 1; i < k; ++i)
    expect(compVerts[static_cast<size_t>(i - 1)].front() <
               compVerts[static_cast<size_t>(i)].front(),
           "components not ordered by smallest vertex id");

  // --- Cycle witnesses ----------------------------------------------------
  // Edge lookup including self-loops.
  std::vector<std::set<int>> adjSet(static_cast<size_t>(n));
  for (int u = 0; u < n; ++u)
    for (int v : g.adj[u]) adjSet[static_cast<size_t>(u)].insert(v);

  for (int i = 0; i < k; ++i) {
    const scc::Json& c = comps[static_cast<size_t>(i)];
    const auto& verts = compVerts[static_cast<size_t>(i)];
    const int s = verts.front();
    const bool selfLoop = adjSet[static_cast<size_t>(s)].count(s) > 0 ||
                          [&] {
                            for (int v : verts)
                              if (adjSet[static_cast<size_t>(v)].count(v))
                                return true;
                            return false;
                          }();
    const scc::Json* w = c.find("cycle_witness");
    expect(w != nullptr, "cycle_witness field missing");
    if (verts.size() == 1 && !selfLoop) {
      expect(w->isNull(),
             "singleton without self-loop must have null witness");
      continue;
    }
    expect(w->isArray(), "cycle_witness must be array or null");
    const auto& path = w->asArray();
    expect(path.size() >= 2, "witness cycle too short");
    std::vector<int> cyc;
    for (const scc::Json& jv : path) {
      expect(jv.isInt(), "witness vertex must be integer");
      cyc.push_back(static_cast<int>(jv.asInt()));
    }
    expect(cyc.front() == cyc.back(), "witness must be a closed cycle");
    std::set<int> seen;
    for (size_t t = 0; t + 1 < cyc.size(); ++t) {
      const int u = cyc[t];
      const int v = cyc[t + 1];
      expect(v2c[static_cast<size_t>(u)] == i,
             "witness vertex outside its component");
      expect(adjSet[static_cast<size_t>(u)].count(v) > 0,
             "witness uses non-existent edge " + std::to_string(u) + "->" +
                 std::to_string(v));
      expect(seen.insert(u).second, "witness is not a simple cycle");
    }
  }

  // --- Condensation edges: exact deduplicated set -------------------------
  std::set<std::pair<int, int>> expected;
  for (int u = 0; u < n; ++u)
    for (int v : g.adj[u]) {
      const int cu = v2c[static_cast<size_t>(u)];
      const int cv = v2c[static_cast<size_t>(v)];
      if (cu != cv) expected.emplace(cu, cv);
    }
  std::set<std::pair<int, int>> actual;
  for (const scc::Json& je : condJson->asArray()) {
    expect(je.isArray() && je.asArray().size() == 2,
           "condensation edge must be a pair");
    const scc::Json& a = je.asArray()[0];
    const scc::Json& b = je.asArray()[1];
    expect(a.isInt() && b.isInt(), "condensation edge endpoints must be int");
    const int cu = static_cast<int>(a.asInt());
    const int cv = static_cast<int>(b.asInt());
    expect(cu >= 0 && cu < k && cv >= 0 && cv < k,
           "condensation edge endpoint out of range");
    expect(cu != cv, "condensation has a self edge");
    expect(actual.insert({cu, cv}).second,
           "condensation edges not deduplicated");
  }
  expect(actual == expected,
         "condensation edge set does not match the exact deduplicated set");
  {
    // Sorted order check (deterministic output).
    std::vector<std::pair<int, int>> sorted(actual.begin(), actual.end());
    size_t idx = 0;
    for (const scc::Json& je : condJson->asArray()) {
      const int cu = static_cast<int>(je.asArray()[0].asInt());
      const int cv = static_cast<int>(je.asArray()[1].asInt());
      expect(idx < sorted.size() && sorted[idx].first == cu &&
                 sorted[idx].second == cv,
             "condensation edges not in sorted order");
      ++idx;
    }
  }

  // --- Condensation is a DAG (Kahn topological sort) ----------------------
  {
    std::vector<int> indeg(static_cast<size_t>(k), 0);
    std::vector<std::vector<int>> cadj(static_cast<size_t>(k));
    for (const auto& e : actual) {
      cadj[static_cast<size_t>(e.first)].push_back(e.second);
      ++indeg[static_cast<size_t>(e.second)];
    }
    std::queue<int> q;
    for (int i = 0; i < k; ++i)
      if (indeg[static_cast<size_t>(i)] == 0) q.push(i);
    int visited = 0;
    while (!q.empty()) {
      const int u = q.front();
      q.pop();
      ++visited;
      for (int v : cadj[static_cast<size_t>(u)])
        if (--indeg[static_cast<size_t>(v)] == 0) q.push(v);
    }
    expect(visited == k, "condensation graph contains a cycle");
  }

  // --- Maximality (small graphs): same comp <=> mutually reachable --------
  bool maximalityChecked = false;
  if (n <= kMaximalityCheckMaxVertices) {
    const int words = (n + 63) / 64;
    std::vector<std::vector<uint64_t>> reach(
        static_cast<size_t>(n),
        std::vector<uint64_t>(static_cast<size_t>(words), 0));
    for (int i = 0; i < n; ++i)
      reach[i][static_cast<size_t>(i) / 64] |= 1ULL << (i % 64);
    for (int u = 0; u < n; ++u)
      for (int v : g.adj[u])
        reach[u][static_cast<size_t>(v) / 64] |= 1ULL << (v % 64);
    for (int kk = 0; kk < n; ++kk) {
      const auto& rowK = reach[kk];
      const uint64_t kBit = 1ULL << (kk % 64);
      const size_t kWord = static_cast<size_t>(kk) / 64;
      for (int i = 0; i < n; ++i)
        if (reach[i][kWord] & kBit)
          for (int w = 0; w < words; ++w) reach[i][w] |= rowK[w];
    }
    for (int i = 0; i < n; ++i)
      for (int j = 0; j < n; ++j) {
        const bool mutual =
            ((reach[i][static_cast<size_t>(j) / 64] >> (j % 64)) & 1ULL) &&
            ((reach[j][static_cast<size_t>(i) / 64] >> (i % 64)) & 1ULL);
        const bool sameComp = v2c[static_cast<size_t>(i)] ==
                              v2c[static_cast<size_t>(j)];
        expect(mutual == sameComp,
               "maximality violated at vertices " + std::to_string(i) +
                   ", " + std::to_string(j));
      }
    maximalityChecked = true;
  }

  std::cout << "VERIFY OK: vertices=" << n << " components=" << k
            << " condensation_edges=" << actual.size()
            << " dag=true maximality="
            << (maximalityChecked ? "checked" : "skipped(n>3000)") << "\n";
  return 0;
}
