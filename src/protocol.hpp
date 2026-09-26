// JSON request/response protocol: parsing, size-limit validation, solving
// and independent re-checking of the optimality certificate.
#pragma once

#include <string>
#include <vector>

#include "bipartite.hpp"
#include "json.hpp"

namespace protocol {

// Instance size limits. The core solver handles much larger graphs, but
// the service caps inputs to bound memory and request time.
constexpr size_t kMaxVerticesPerSide = 10000;
constexpr size_t kMaxEdges = 200000;
constexpr long long kMaxId = 9007199254740992LL;   // 2^53, safe integer
constexpr long long kMinId = -9007199254740992LL;

struct Request {
  std::vector<long long> leftVertices;
  std::vector<long long> rightVertices;
  std::vector<bipartite::Edge> edges;
  bool bruteForce = false;
  bool includeReachable = true;
};

struct ErrorInfo {
  std::string code;
  std::string message;
};

// Parses and validates a single JSON request. On failure returns false and
// fills `error`.
bool parseRequest(const json::Value& root, Request& request, ErrorInfo& error);

// Solves the request and returns a fully formed JSON response value
// (success or structured error).
json::Value handleRequest(const json::Value& root);

// Convenience overload for an already-parsed request.
json::Value solveRequest(const Request& request);

}  // namespace protocol
