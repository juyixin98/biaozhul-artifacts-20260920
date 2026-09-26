#include "api.hpp"

#include <cmath>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_map>

#include "matching.hpp"

namespace {

// A vertex ID may be a string or an integer number; it is echoed back
// verbatim in the response so the caller keeps their original IDs.
std::string idKey(const JsonValue& id) {
  if (id.isString()) return "s:" + id.str;
  if (id.isNumber()) {
    double n = id.number;
    if (n != std::floor(n)) {
      throw std::runtime_error("vertex IDs must be strings or integers");
    }
    return "n:" + std::to_string(static_cast<long long>(n));
  }
  throw std::runtime_error("vertex IDs must be strings or integers, got " +
                           id.typeName());
}

struct IndexedGraph {
  std::vector<JsonValue> leftIds;   // index -> original ID
  std::vector<JsonValue> rightIds;  // index -> original ID
  std::vector<std::vector<int>> adj;
  long long duplicateEdges = 0;
  long long edgeCount = 0;  // after deduplication
};

const JsonValue& requireMember(const JsonValue& obj, const char* key) {
  const JsonValue* v = obj.find(key);
  if (!v) throw std::runtime_error(std::string("missing required field '") +
                                   key + "'");
  return *v;
}

std::vector<JsonValue> parseSide(const JsonValue& request, const char* key,
                                 const char* sideName) {
  const JsonValue& side = requireMember(request, key);
  if (!side.isArray()) {
    throw std::runtime_error(std::string("field '") + key +
                             "' must be an array of vertex IDs");
  }
  std::vector<JsonValue> ids;
  std::set<std::string> seen;
  for (const JsonValue& id : side.arr) {
    std::string k = idKey(id);
    if (!seen.insert(k).second) {
      throw std::runtime_error(std::string("duplicate vertex ID in '") + key +
                               "': " + id.dump());
    }
    ids.push_back(id);
  }
  if (static_cast<long long>(ids.size()) > kMaxVerticesPerSide) {
    throw std::runtime_error(std::string("too many ") + sideName +
                             " vertices (limit " +
                             std::to_string(kMaxVerticesPerSide) + ")");
  }
  return ids;
}

IndexedGraph buildGraph(const JsonValue& request) {
  if (!request.isObject()) {
    throw std::runtime_error("request must be a JSON object");
  }
  IndexedGraph g;
  g.leftIds = parseSide(request, "left", "left");
  g.rightIds = parseSide(request, "right", "right");

  std::unordered_map<std::string, int> leftIndex, rightIndex;
  for (size_t i = 0; i < g.leftIds.size(); ++i) {
    leftIndex[idKey(g.leftIds[i])] = static_cast<int>(i);
  }
  for (size_t i = 0; i < g.rightIds.size(); ++i) {
    rightIndex[idKey(g.rightIds[i])] = static_cast<int>(i);
  }

  const JsonValue& edges = requireMember(request, "edges");
  if (!edges.isArray()) {
    throw std::runtime_error("field 'edges' must be an array of [left, right] pairs");
  }
  if (static_cast<long long>(edges.arr.size()) > kMaxEdges) {
    throw std::runtime_error("too many edges (limit " +
                             std::to_string(kMaxEdges) + ")");
  }

  g.adj.assign(g.leftIds.size(), {});
  std::set<std::pair<int, int>> seenEdges;
  for (const JsonValue& e : edges.arr) {
    if (!e.isArray() || e.arr.size() != 2) {
      throw std::runtime_error("each edge must be a [left, right] pair");
    }
    std::string lk = idKey(e.arr[0]);
    std::string rk = idKey(e.arr[1]);
    auto li = leftIndex.find(lk);
    if (li == leftIndex.end()) {
      throw std::runtime_error("edge references unknown left vertex: " +
                               e.arr[0].dump());
    }
    auto ri = rightIndex.find(rk);
    if (ri == rightIndex.end()) {
      throw std::runtime_error("edge references unknown right vertex: " +
                               e.arr[1].dump());
    }
    std::pair<int, int> edge{li->second, ri->second};
    if (!seenEdges.insert(edge).second) {
      ++g.duplicateEdges;  // duplicate edge: ignored for matching
      continue;
    }
    g.adj[edge.first].push_back(edge.second);
  }
  g.edgeCount = static_cast<long long>(seenEdges.size());
  return g;
}

// Independent re-check of the certificate, run before responding so the
// "verified" flags in the output are actual evidence, not claims.
bool matchingIsValid(const IndexedGraph& g, const MatchingResult& r) {
  std::set<std::pair<int, int>> edgeSet;
  for (int u = 0; u < r.nLeft; ++u) {
    for (int v : g.adj[u]) edgeSet.insert({u, v});
  }
  std::vector<bool> usedL(r.nLeft, false), usedR(r.nRight, false);
  for (int u = 0; u < r.nLeft; ++u) {
    int v = r.matchLeft[u];
    if (v < 0) continue;
    if (v >= r.nRight || usedL[u] || usedR[v]) return false;
    if (r.matchRight[v] != u) return false;
    if (!edgeSet.count({u, v})) return false;
    usedL[u] = usedR[v] = true;
  }
  return true;
}

bool coverCoversAllEdges(const IndexedGraph& g, const MatchingResult& r) {
  for (int u = 0; u < r.nLeft; ++u) {
    for (int v : g.adj[u]) {
      if (!r.coverLeft[u] && !r.coverRight[v]) return false;
    }
  }
  return true;
}

JsonValue idArray(const std::vector<JsonValue>& ids,
                  const std::vector<bool>& picked) {
  JsonValue out = JsonValue::makeArray();
  for (size_t i = 0; i < ids.size(); ++i) {
    if (picked[i]) out.arr.push_back(ids[i]);
  }
  return out;
}

}  // namespace

JsonValue solveRequest(const JsonValue& request) {
  IndexedGraph g;
  try {
    g = buildGraph(request);
  } catch (const std::exception& ex) {
    JsonValue err = JsonValue::makeObject();
    err.obj.emplace_back("ok", JsonValue::makeBool(false));
    err.obj.emplace_back("error", JsonValue::makeString(ex.what()));
    return err;
  }

  MatchingResult r = bipartiteMaxMatching(
      static_cast<int>(g.leftIds.size()), static_cast<int>(g.rightIds.size()),
      g.adj);

  bool validMatching = matchingIsValid(g, r);
  bool coversAll = coverCoversAllEdges(g, r);
  bool sizeMatches = (r.coverSize == r.matchingSize);
  bool verified = validMatching && coversAll && sizeMatches;

  JsonValue matching = JsonValue::makeArray();
  for (int u = 0; u < r.nLeft; ++u) {
    int v = r.matchLeft[u];
    if (v < 0) continue;
    JsonValue pair = JsonValue::makeObject();
    pair.obj.emplace_back("left", g.leftIds[u]);
    pair.obj.emplace_back("right", g.rightIds[v]);
    matching.arr.push_back(std::move(pair));
  }

  JsonValue cover = JsonValue::makeObject();
  cover.obj.emplace_back("left", idArray(g.leftIds, r.coverLeft));
  cover.obj.emplace_back("right", idArray(g.rightIds, r.coverRight));
  cover.obj.emplace_back("size", JsonValue::makeNumber(r.coverSize));

  JsonValue certificate = JsonValue::makeObject();
  certificate.obj.emplace_back("theorem",
                               JsonValue::makeString("konig"));
  certificate.obj.emplace_back("matching_valid",
                               JsonValue::makeBool(validMatching));
  certificate.obj.emplace_back("cover_covers_all_edges",
                               JsonValue::makeBool(coversAll));
  certificate.obj.emplace_back("cover_size_equals_matching_size",
                               JsonValue::makeBool(sizeMatches));

  JsonValue stats = JsonValue::makeObject();
  stats.obj.emplace_back("left_vertices",
                         JsonValue::makeNumber(g.leftIds.size()));
  stats.obj.emplace_back("right_vertices",
                         JsonValue::makeNumber(g.rightIds.size()));
  stats.obj.emplace_back("edges", JsonValue::makeNumber(g.edgeCount));
  stats.obj.emplace_back("duplicate_edges_ignored",
                         JsonValue::makeNumber(g.duplicateEdges));

  JsonValue resp = JsonValue::makeObject();
  resp.obj.emplace_back("ok", JsonValue::makeBool(true));
  resp.obj.emplace_back("verified", JsonValue::makeBool(verified));
  resp.obj.emplace_back("matching_size", JsonValue::makeNumber(r.matchingSize));
  resp.obj.emplace_back("matching", std::move(matching));
  resp.obj.emplace_back("vertex_cover", std::move(cover));
  resp.obj.emplace_back("certificate", std::move(certificate));
  resp.obj.emplace_back("stats", std::move(stats));
  return resp;
}
