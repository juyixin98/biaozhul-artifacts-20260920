#include "protocol.hpp"

#include <algorithm>
#include <chrono>
#include <set>
#include <unordered_set>

#include "brute.hpp"

namespace protocol {

namespace {

json::Value makeError(const std::string& code, const std::string& message) {
  json::Value root = json::Value::makeObject();
  json::Value error = json::Value::makeObject();
  error.members.emplace_back("code", json::Value::makeString(code));
  error.members.emplace_back("message", json::Value::makeString(message));
  root.members.emplace_back("ok", json::Value::makeBoolean(false));
  root.members.emplace_back("error", error);
  return root;
}

bool asId(const json::Value& value, long long& out) {
  if (!value.isInteger()) return false;
  out = value.integer;
  return out >= kMinId && out <= kMaxId;
}

bool readIdArray(const json::Value* arrayValue,
                 std::vector<long long>& out,
                 const std::string& field,
                 ErrorInfo& error) {
  if (arrayValue == nullptr) return true;
  if (!arrayValue->isArray()) {
    error = {"INVALID_REQUEST", "field '" + field + "' must be an array"};
    return false;
  }
  for (const json::Value& item : arrayValue->items) {
    long long id;
    if (!asId(item, id)) {
      error = {"INVALID_REQUEST",
               "field '" + field +
                   "' contains a value that is not an integer within "
                   "[-2^53, 2^53]"};
      return false;
    }
    out.push_back(id);
  }
  return true;
}

// Independently re-checks the returned matching and cover directly against
// the input edges. No result of the solver feeds this check.
struct CheckResult {
  bool matchingValid = false;
  bool coversAllEdges = false;
  bool sizesEqual = false;
  bool verified = false;
};

CheckResult verifyCertificate(
    const std::vector<bipartite::Edge>& uniqueEdges,
    const std::vector<bipartite::Edge>& matching,
    const std::vector<bipartite::Solution::CoverVertex>& cover) {
  CheckResult result;

  std::set<std::pair<long long, long long>> edgeSet;
  for (const bipartite::Edge& edge : uniqueEdges) {
    edgeSet.emplace(edge.left, edge.right);
  }

  // 1. Matching: pairs are real edges; no vertex is used twice.
  std::unordered_set<long long> usedLeft;
  std::unordered_set<long long> usedRight;
  result.matchingValid = true;
  for (const bipartite::Edge& pair : matching) {
    if (!edgeSet.count({pair.left, pair.right}) ||
        !usedLeft.insert(pair.left).second ||
        !usedRight.insert(pair.right).second) {
      result.matchingValid = false;
      break;
    }
  }

  // 2. Cover: every edge has an endpoint in the cover.
  std::unordered_set<long long> coveredLeft;
  std::unordered_set<long long> coveredRight;
  for (const auto& vertex : cover) {
    if (vertex.side == 'L') coveredLeft.insert(vertex.id);
    else coveredRight.insert(vertex.id);
  }
  result.coversAllEdges = std::all_of(
      uniqueEdges.begin(), uniqueEdges.end(),
      [&](const bipartite::Edge& edge) {
        return coveredLeft.count(edge.left) || coveredRight.count(edge.right);
      });

  // 3. Cardinality equality with the matching (both are optima by Konig).
  result.sizesEqual = cover.size() == matching.size();
  result.verified =
      result.matchingValid && result.coversAllEdges && result.sizesEqual;
  return result;
}

json::Value idArrayToJson(const std::vector<long long>& ids) {
  json::Value array = json::Value::makeArray();
  array.items.reserve(ids.size());
  for (long long id : ids) array.items.push_back(json::Value::makeInteger(id));
  return array;
}

}  // namespace

bool parseRequest(const json::Value& root, Request& request,
                  ErrorInfo& error) {
  if (!root.isObject()) {
    error = {"INVALID_REQUEST", "request body must be a JSON object"};
    return false;
  }

  if (!readIdArray(root.find("left"), request.leftVertices, "left", error) ||
      !readIdArray(root.find("right"), request.rightVertices, "right",
                   error)) {
    return false;
  }

  const json::Value* edgesValue = root.find("edges");
  if (edgesValue == nullptr) {
    request.edges = {};
  } else {
    if (!edgesValue->isArray()) {
      error = {"INVALID_REQUEST", "field 'edges' must be an array"};
      return false;
    }
    if (edgesValue->items.size() > kMaxEdges) {
      error = {"LIMIT_EXCEEDED",
               "too many edges: limit is " + std::to_string(kMaxEdges)};
      return false;
    }
    for (const json::Value& item : edgesValue->items) {
      long long leftId;
      long long rightId;
      // Accepted edge forms: {"left": u, "right": v} or [u, v].
      if (item.isObject()) {
        const json::Value* leftField = item.find("left");
        const json::Value* rightField = item.find("right");
        if (leftField == nullptr || rightField == nullptr ||
            !asId(*leftField, leftId) || !asId(*rightField, rightId)) {
          error = {"INVALID_REQUEST",
                   "each edge must be {\"left\": int, \"right\": int}"};
          return false;
        }
      } else if (item.isArray() && item.items.size() == 2) {
        if (!asId(item.items[0], leftId) ||
            !asId(item.items[1], rightId)) {
          error = {"INVALID_REQUEST",
                   "each edge pair must contain two integer ids"};
          return false;
        }
      } else {
        error = {"INVALID_REQUEST",
                 "each edge must be an object {\"left\", \"right\"} or a "
                 "[u, v] pair"};
        return false;
      }
      request.edges.push_back(bipartite::Edge{leftId, rightId});
    }
  }

  const json::Value* bruteValue = root.find("brute_force");
  if (bruteValue != nullptr) {
    if (!bruteValue->isBoolean()) {
      error = {"INVALID_REQUEST", "field 'brute_force' must be a boolean"};
      return false;
    }
    request.bruteForce = bruteValue->boolean;
  }
  const json::Value* reachableValue = root.find("include_reachable");
  if (reachableValue != nullptr) {
    if (!reachableValue->isBoolean()) {
      error = {"INVALID_REQUEST",
               "field 'include_reachable' must be a boolean"};
      return false;
    }
    request.includeReachable = reachableValue->boolean;
  }

  // Reject an id declared on both sides: the bipartition would be
  // ambiguous.
  std::set<long long> leftSet(request.leftVertices.begin(),
                              request.leftVertices.end());
  std::set<long long> rightSet(request.rightVertices.begin(),
                               request.rightVertices.end());
  std::vector<long long> overlap;
  std::set_intersection(leftSet.begin(), leftSet.end(), rightSet.begin(),
                        rightSet.end(), std::back_inserter(overlap));
  if (!overlap.empty()) {
    error = {"INVALID_REQUEST",
             "vertex id " + std::to_string(overlap.front()) +
                 " is declared on both sides; a bipartition requires "
                 "disjoint id sets"};
    return false;
  }

  // If sides are declared explicitly, every edge endpoint must belong to
  // the declared side (this is what makes isolated vertices expressible).
  bool sidesDeclared = root.find("left") != nullptr ||
                       root.find("right") != nullptr;
  if (sidesDeclared) {
    for (const bipartite::Edge& edge : request.edges) {
      if (!leftSet.count(edge.left)) {
        error = {"INVALID_REQUEST",
                 "edge endpoint " + std::to_string(edge.left) +
                     " is not listed in 'left'"};
        return false;
      }
      if (!rightSet.count(edge.right)) {
        error = {"INVALID_REQUEST",
                 "edge endpoint " + std::to_string(edge.right) +
                     " is not listed in 'right'"};
        return false;
      }
    }
  }

  if (leftSet.size() > kMaxVerticesPerSide ||
      rightSet.size() > kMaxVerticesPerSide) {
    error = {"LIMIT_EXCEEDED",
             "too many vertices on one side: limit is " +
                 std::to_string(kMaxVerticesPerSide)};
    return false;
  }
  return true;
}

json::Value solveRequest(const Request& request) {
  auto started = std::chrono::steady_clock::now();

  json::Value warnings = json::Value::makeArray();
  if (std::set<long long>(request.leftVertices.begin(),
                          request.leftVertices.end())
          .size() != request.leftVertices.size() ||
      std::set<long long>(request.rightVertices.begin(),
                          request.rightVertices.end())
          .size() != request.rightVertices.size()) {
    warnings.items.push_back(json::Value::makeString(
        "duplicate ids in 'left'/'right' were deduplicated"));
  }

  bipartite::Matcher matcher(request.edges, request.leftVertices,
                             request.rightVertices);
  if (matcher.leftCount() > kMaxVerticesPerSide ||
      matcher.rightCount() > kMaxVerticesPerSide) {
    return makeError("LIMIT_EXCEEDED",
                     "too many distinct vertices on one side: limit is " +
                         std::to_string(kMaxVerticesPerSide));
  }
  if (matcher.edgeCount() > kMaxEdges) {
    return makeError(
        "LIMIT_EXCEEDED",
        "too many distinct edges: limit is " + std::to_string(kMaxEdges));
  }

  const bipartite::Solution& solution = matcher.solve();

  std::vector<bipartite::Edge> uniqueEdges;
  uniqueEdges.reserve(request.edges.size());
  {
    std::set<std::pair<long long, long long>> seen;
    for (const bipartite::Edge& edge : request.edges) {
      if (seen.emplace(edge.left, edge.right).second) {
        uniqueEdges.push_back(edge);
      }
    }
    std::sort(uniqueEdges.begin(), uniqueEdges.end(),
              [](const bipartite::Edge& a, const bipartite::Edge& b) {
                if (a.left != b.left) return a.left < b.left;
                return a.right < b.right;
              });
  }
  if (uniqueEdges.size() != request.edges.size()) {
    warnings.items.push_back(json::Value::makeString(
        "duplicate edges in the request were treated as one"));
  }

  CheckResult check =
      verifyCertificate(uniqueEdges, solution.matching,
                        solution.minVertexCover);

  // Optional naive exhaustive cross-check (small instances only).
  json::Value bruteBlock;
  bool hasBruteBlock = false;
  if (request.bruteForce) {
    // Declared ids plus every edge endpoint (the same id universe the
    // core solver compresses internally).
    std::vector<long long> allLeft = request.leftVertices;
    std::vector<long long> allRight = request.rightVertices;
    for (const bipartite::Edge& edge : uniqueEdges) {
      allLeft.push_back(edge.left);
      allRight.push_back(edge.right);
    }
    std::sort(allLeft.begin(), allLeft.end());
    allLeft.erase(std::unique(allLeft.begin(), allLeft.end()),
                  allLeft.end());
    std::sort(allRight.begin(), allRight.end());
    allRight.erase(std::unique(allRight.begin(), allRight.end()),
                   allRight.end());

    if (!brute::withinLimits(allLeft.size(), allRight.size(),
                             uniqueEdges.size())) {
      return makeError(
          "BRUTE_FORCE_TOO_LARGE",
          "brute_force reference supports at most " +
              std::to_string(brute::kMaxTotalVertices) +
              " total vertices and " + std::to_string(brute::kMaxEdges) +
              " edges; this instance has " +
              std::to_string(allLeft.size() + allRight.size()) +
              " vertices and " + std::to_string(uniqueEdges.size()) +
              " edges");
    }
    size_t bruteMatching = brute::exhaustiveMatchingSize(
        uniqueEdges, allLeft, allRight);
    brute::ReferenceResult bruteCover = brute::exhaustiveMinVertexCover(
        uniqueEdges, allLeft, allRight);
    bruteBlock = json::Value::makeObject();
    bruteBlock.members.emplace_back(
        "max_matching_size",
        json::Value::makeInteger(static_cast<long long>(bruteMatching)));
    bruteBlock.members.emplace_back(
        "min_vertex_cover_size",
        json::Value::makeInteger(
            static_cast<long long>(bruteCover.minVertexCoverSize)));
    bool agrees = bruteMatching == solution.matchingSize &&
                  bruteCover.minVertexCoverSize == solution.coverSize;
    bruteBlock.members.emplace_back(
        "agrees", json::Value::makeBoolean(agrees));
    hasBruteBlock = true;
  }

  json::Value root = json::Value::makeObject();
  root.members.emplace_back("ok", json::Value::makeBoolean(true));

  json::Value graphInfo = json::Value::makeObject();
  graphInfo.members.emplace_back(
      "left_count",
      json::Value::makeInteger(static_cast<long long>(matcher.leftCount())));
  graphInfo.members.emplace_back(
      "right_count",
      json::Value::makeInteger(static_cast<long long>(matcher.rightCount())));
  graphInfo.members.emplace_back(
      "edge_count",
      json::Value::makeInteger(static_cast<long long>(uniqueEdges.size())));
  root.members.emplace_back("graph", graphInfo);

  root.members.emplace_back(
      "matching_size",
      json::Value::makeInteger(
          static_cast<long long>(solution.matchingSize)));
  json::Value matchingJson = json::Value::makeArray();
  for (const bipartite::Edge& pair : solution.matching) {
    json::Value edge = json::Value::makeObject();
    edge.members.emplace_back("left",
                              json::Value::makeInteger(pair.left));
    edge.members.emplace_back("right",
                              json::Value::makeInteger(pair.right));
    matchingJson.items.push_back(edge);
  }
  root.members.emplace_back("matching", matchingJson);

  root.members.emplace_back(
      "minimum_vertex_cover_size",
      json::Value::makeInteger(
          static_cast<long long>(solution.coverSize)));
  json::Value coverJson = json::Value::makeArray();
  for (const auto& vertex : solution.minVertexCover) {
    json::Value vertexJson = json::Value::makeObject();
    vertexJson.members.emplace_back(
        "side", json::Value::makeString(std::string(1, vertex.side)));
    vertexJson.members.emplace_back("id",
                                    json::Value::makeInteger(vertex.id));
    coverJson.items.push_back(vertexJson);
  }
  root.members.emplace_back("minimum_vertex_cover", coverJson);

  if (request.includeReachable) {
    json::Value reachable = json::Value::makeObject();
    reachable.members.emplace_back("left",
                                   idArrayToJson(solution.reachableLeft));
    reachable.members.emplace_back("right",
                                   idArrayToJson(solution.reachableRight));
    root.members.emplace_back("alternating_reachable", reachable);
  }

  json::Value verification = json::Value::makeObject();
  verification.members.emplace_back(
      "matching_is_valid",
      json::Value::makeBoolean(check.matchingValid));
  verification.members.emplace_back(
      "cover_touches_every_edge",
      json::Value::makeBoolean(check.coversAllEdges));
  verification.members.emplace_back(
      "cover_size_equals_matching_size",
      json::Value::makeBoolean(check.sizesEqual));
  verification.members.emplace_back(
      "verified_optimal", json::Value::makeBoolean(check.verified));
  verification.members.emplace_back(
      "argument",
      json::Value::makeString(
          "Every edge touches the cover (feasibility), and the cover "
          "size equals the matching size. A matching is a lower bound "
          "on any vertex cover, so a feasible cover of the same size is "
          "minimum and the matching is maximum (Kőnig's theorem)."));
  root.members.emplace_back("verification", verification);

  if (hasBruteBlock) root.members.emplace_back("brute_reference", bruteBlock);
  root.members.emplace_back("warnings", warnings);

  auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
                     std::chrono::steady_clock::now() - started)
                     .count();
  root.members.emplace_back(
      "elapsed_us", json::Value::makeInteger(static_cast<long long>(elapsed)));
  return root;
}

json::Value handleRequest(const json::Value& root) {
  Request request;
  ErrorInfo error;
  if (!parseRequest(root, request, error)) {
    return makeError(error.code, error.message);
  }
  return solveRequest(request);
}

}  // namespace protocol
