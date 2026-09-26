#include "scc.hpp"

#include <algorithm>
#include <queue>
#include <set>

namespace scc {
namespace {

// Builds the final result (numbering, witnesses, condensation edges) from a
// list of components given as vertex lists. Shared by the Tarjan solver and
// the naive reference so both produce identically-shaped output.
SccResult finalizeResult(const Graph& g, std::vector<std::vector<int>> comps) {
  const int n = g.vertexCount;
  for (auto& c : comps) std::sort(c.begin(), c.end());
  // Deterministic numbering: order components by smallest vertex id.
  std::sort(comps.begin(), comps.end(),
            [](const std::vector<int>& a, const std::vector<int>& b) {
              return a.front() < b.front();
            });

  SccResult result;
  result.vertexToComponent.assign(n, -1);
  result.components.reserve(comps.size());
  for (size_t i = 0; i < comps.size(); ++i) {
    ComponentResult cr;
    cr.id = static_cast<int>(i);
    cr.vertices = std::move(comps[i]);
    for (int v : cr.vertices) result.vertexToComponent[v] = cr.id;
    result.components.push_back(std::move(cr));
  }

  // Cycle witness per component. For a singleton the only possible cycle is
  // a self-loop. For larger components, BFS (ascending neighbor order, so
  // the witness is deterministic) for the shortest path from the minimum
  // vertex back to itself; the BFS tree path is simple by construction.
  std::vector<char> inComp(n, 0);
  for (auto& cr : result.components) {
    const int s = cr.vertices.front();
    for (int v : cr.vertices) inComp[v] = 1;

    const auto& adjS = g.adj[s];
    const bool selfLoop =
        std::binary_search(adjS.begin(), adjS.end(), s);
    if (selfLoop) {
      cr.cycleWitness = {s, s};
    } else if (cr.vertices.size() > 1) {
      std::vector<int> parent(n, -1);
      std::queue<int> q;
      for (int w : adjS) {
        if (inComp[w] && w != s && parent[w] == -1) {
          parent[w] = s;
          q.push(w);
        }
      }
      while (!q.empty() && cr.cycleWitness.empty()) {
        int u = q.front();
        q.pop();
        for (int v : g.adj[u]) {
          if (!inComp[v]) continue;
          if (v == s) {
            std::vector<int> cyc;
            for (int x = u; x != s; x = parent[x]) cyc.push_back(x);
            cyc.push_back(s);
            std::reverse(cyc.begin(), cyc.end());
            cyc.push_back(s);
            cr.cycleWitness = std::move(cyc);
            break;
          }
          if (parent[v] == -1) {
            parent[v] = u;
            q.push(v);
          }
        }
      }
    }

    for (int v : cr.vertices) inComp[v] = 0;
  }

  // Deduplicated condensation edges.
  std::set<std::pair<int, int>> edgeSet;
  for (int u = 0; u < n; ++u) {
    for (int v : g.adj[u]) {
      int cu = result.vertexToComponent[u];
      int cv = result.vertexToComponent[v];
      if (cu != cv) edgeSet.emplace(cu, cv);
    }
  }
  result.condensationEdges.assign(edgeSet.begin(), edgeSet.end());
  return result;
}

}  // namespace

Graph graphFromJson(const Json& req) {
  if (!req.isObject()) throw JsonError("request must be a JSON object");
  const Json* vertices = req.find("vertices");
  if (!vertices || !vertices->isInt())
    throw JsonError("request must contain integer field \"vertices\"");
  const int64_t n = vertices->asInt();
  if (n < 0) throw JsonError("\"vertices\" must be >= 0");
  if (n > kMaxVertices)
    throw JsonError("\"vertices\" exceeds limit " +
                    std::to_string(kMaxVertices));
  const Json* edges = req.find("edges");
  if (!edges || !edges->isArray())
    throw JsonError("request must contain array field \"edges\"");
  const auto& edgeArr = edges->asArray();
  if (static_cast<int64_t>(edgeArr.size()) > kMaxEdges)
    throw JsonError("edge count exceeds limit " + std::to_string(kMaxEdges));

  Graph g;
  g.vertexCount = static_cast<int>(n);
  g.adj.assign(static_cast<size_t>(n), {});
  g.rawEdgeCount = static_cast<int64_t>(edgeArr.size());

  for (size_t i = 0; i < edgeArr.size(); ++i) {
    const Json& e = edgeArr[i];
    if (!e.isArray() || e.asArray().size() != 2)
      throw JsonError("edge #" + std::to_string(i) +
                      " must be a [from, to] pair");
    const Json& ju = e.asArray()[0];
    const Json& jv = e.asArray()[1];
    if (!ju.isInt() || !jv.isInt())
      throw JsonError("edge #" + std::to_string(i) +
                      " endpoints must be integers");
    const int64_t u = ju.asInt();
    const int64_t v = jv.asInt();
    if (u < 0 || u >= n || v < 0 || v >= n)
      throw JsonError("edge #" + std::to_string(i) +
                      " endpoint out of range [0, " + std::to_string(n) + ")");
    g.adj[static_cast<size_t>(u)].push_back(static_cast<int>(v));
  }
  for (auto& lst : g.adj) {
    std::sort(lst.begin(), lst.end());
    lst.erase(std::unique(lst.begin(), lst.end()), lst.end());
  }
  return g;
}

SccResult computeScc(const Graph& g) {
  const int n = g.vertexCount;
  std::vector<int> index(n, -1), low(n, 0);
  std::vector<char> onStack(n, 0);
  std::vector<int> tarjanStack;
  std::vector<std::vector<int>> comps;
  int nextIndex = 0;

  // Iterative Tarjan: explicit call stack avoids recursion depth limits.
  std::vector<int> callStack;
  std::vector<size_t> childPtr;
  for (int s = 0; s < n; ++s) {
    if (index[s] != -1) continue;
    index[s] = low[s] = nextIndex++;
    tarjanStack.push_back(s);
    onStack[s] = 1;
    callStack.push_back(s);
    childPtr.push_back(0);
    while (!callStack.empty()) {
      const int v = callStack.back();
      size_t& i = childPtr.back();
      if (i < g.adj[v].size()) {
        const int w = g.adj[v][i++];
        if (index[w] == -1) {
          index[w] = low[w] = nextIndex++;
          tarjanStack.push_back(w);
          onStack[w] = 1;
          callStack.push_back(w);
          childPtr.push_back(0);
        } else if (onStack[w]) {
          low[v] = std::min(low[v], index[w]);
        }
      } else {
        if (callStack.size() > 1) {
          const int parent = callStack[callStack.size() - 2];
          low[parent] = std::min(low[parent], low[v]);
        }
        if (low[v] == index[v]) {
          std::vector<int> comp;
          while (true) {
            const int w = tarjanStack.back();
            tarjanStack.pop_back();
            onStack[w] = 0;
            comp.push_back(w);
            if (w == v) break;
          }
          comps.push_back(std::move(comp));
        }
        callStack.pop_back();
        childPtr.pop_back();
      }
    }
  }
  return finalizeResult(g, std::move(comps));
}

SccResult computeSccReference(const Graph& g) {
  const int n = g.vertexCount;
  if (n > kReferenceMaxVertices)
    throw JsonError("reference implementation limited to " +
                    std::to_string(kReferenceMaxVertices) + " vertices");
  const int words = (n + 63) / 64;
  // Reachability matrix as bitsets, transitive closure via Warshall.
  std::vector<std::vector<uint64_t>> reach(
      static_cast<size_t>(n), std::vector<uint64_t>(static_cast<size_t>(words), 0));
  for (int i = 0; i < n; ++i)
    reach[i][static_cast<size_t>(i) / 64] |= 1ULL << (i % 64);
  for (int u = 0; u < n; ++u)
    for (int v : g.adj[u])
      reach[u][static_cast<size_t>(v) / 64] |= 1ULL << (v % 64);
  for (int k = 0; k < n; ++k) {
    const auto& rowK = reach[k];
    const uint64_t kBit = 1ULL << (k % 64);
    const size_t kWord = static_cast<size_t>(k) / 64;
    for (int i = 0; i < n; ++i) {
      if (reach[i][kWord] & kBit) {
        auto& rowI = reach[i];
        for (int w = 0; w < words; ++w) rowI[w] |= rowK[w];
      }
    }
  }
  // SCCs = mutual-reachability classes.
  std::vector<char> assigned(static_cast<size_t>(n), 0);
  std::vector<std::vector<int>> comps;
  for (int i = 0; i < n; ++i) {
    if (assigned[i]) continue;
    std::vector<int> comp;
    for (int j = 0; j < n; ++j) {
      const bool ij =
          (reach[i][static_cast<size_t>(j) / 64] >> (j % 64)) & 1ULL;
      const bool ji =
          (reach[j][static_cast<size_t>(i) / 64] >> (i % 64)) & 1ULL;
      if (ij && ji) {
        comp.push_back(j);
        assigned[j] = 1;
      }
    }
    comps.push_back(std::move(comp));
  }
  return finalizeResult(g, std::move(comps));
}

Json resultToJson(const SccResult& r, const Graph& g, bool usedReference) {
  Json::Array comps;
  for (const auto& c : r.components) {
    Json::Array verts;
    for (int v : c.vertices) verts.push_back(Json(v));
    Json witness;
    if (!c.cycleWitness.empty()) {
      Json::Array w;
      for (int v : c.cycleWitness) w.push_back(Json(v));
      witness = Json(std::move(w));
    }
    comps.push_back(Json(Json::Object{
        {"cycle_witness", std::move(witness)},
        {"id", Json(c.id)},
        {"size", Json(static_cast<int64_t>(c.vertices.size()))},
        {"vertices", std::move(verts)},
    }));
  }
  Json::Array condEdges;
  for (const auto& e : r.condensationEdges) {
    condEdges.push_back(
        Json(Json::Array{Json(e.first), Json(e.second)}));
  }
  return Json(Json::Object{
      {"algorithm",
       Json(usedReference ? "reachability-reference" : "tarjan-iterative")},
      {"component_count", Json(static_cast<int64_t>(r.components.size()))},
      {"components", std::move(comps)},
      {"condensation_edge_count",
       Json(static_cast<int64_t>(r.condensationEdges.size()))},
      {"condensation_edges", std::move(condEdges)},
      {"edge_count", Json(g.rawEdgeCount)},
      {"numbering",
       Json("components ordered by smallest contained vertex id")},
      {"ok", Json(true)},
      {"vertex_count", Json(static_cast<int64_t>(g.vertexCount))},
  });
}

}  // namespace scc
