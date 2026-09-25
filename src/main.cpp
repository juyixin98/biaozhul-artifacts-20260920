// JSON request entry point for the spatial nearest-neighbour index.
//
// Usage:
//   spatial_index [request.json | -] [response.json]
// With no arguments (or "-") the request is read from stdin; the response is
// written to stdout unless a second path is given. Malformed requests produce
// a JSON error object on stdout and exit status 1.
//
// Request schema:
//   {
//     "points":  [ {"id": <int>, "x": <number>, "y": <number>}, ... ],
//     "queries": [ {"type": "knn",    "x": ..., "y": ..., "k": <int >= 0>},
//                  {"type": "radius", "x": ..., "y": ..., "r": <number >= 0>} ]
//   }
// Points may share coordinates (duplicates are indexed independently) but ids
// must be unique integers, because id is the distance-tie breaker.
//
// Response: {"ok": true, "indexed": N, "results": [ ... ]}
// Each neighbor exposes "id", "distance_squared" and "distance".
#include <algorithm>
#include <cmath>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <regex>
#include <sstream>
#include <string>
#include <vector>

#include "json.hpp"
#include "kdtree.hpp"

namespace {

using spatial::Coord;
using spatial::Id;
using spatial::KdTree;
using spatial::Neighbor;
using spatial::Point;

std::string fail(const std::string& msg) {
  json::Value resp = json::Value::makeObject();
  resp.members.emplace_back("ok", json::Value::makeBool(false));
  resp.members.emplace_back("error", json::Value::makeString(msg));
  return json::dump(resp);
}

// Extracts an exact integer from a JSON number token. strtod would silently
// lose precision beyond 2^53, so the raw token is validated and parsed with
// strtoll.
bool exactInt(const json::Value& v, std::int64_t& out, std::string& err) {
  if (!v.is(json::Value::Type::Number) || v.rawNumber.empty()) {
    err = "expected an integer value";
    return false;
  }
  static const std::regex intRe(R"(-?(0|[1-9][0-9]*))");
  if (!std::regex_match(v.rawNumber, intRe)) {
    err = "value '" + v.rawNumber + "' is not an integer";
    return false;
  }
  errno = 0;
  char* end = nullptr;
  long long parsed = std::strtoll(v.rawNumber.c_str(), &end, 10);
  if (errno == ERANGE || end != v.rawNumber.c_str() + v.rawNumber.size()) {
    err = "integer '" + v.rawNumber + "' is outside the int64 range";
    return false;
  }
  out = static_cast<std::int64_t>(parsed);
  return true;
}

bool finiteNumber(const json::Value& v, long double& out, std::string& err) {
  if (!v.is(json::Value::Type::Number)) {
    err = "expected a numeric value";
    return false;
  }
  if (!std::isfinite(v.number)) {  // parser already rejects 1e999 et al.
    err = "non-finite number in request";
    return false;
  }
  out = v.number;
  return true;
}

json::Value neighborJson(const Neighbor& n) {
  json::Value o = json::Value::makeObject();
  o.members.emplace_back("id", json::Value::makeInt(n.id));
  o.members.emplace_back("distance_squared", json::Value::makeNumber(n.dist2));
  o.members.emplace_back("distance", json::Value::makeNumber(std::sqrt(n.dist2)));
  return o;
}

}  // namespace

int main(int argc, char** argv) {
  std::string inPath = argc > 1 ? argv[1] : "-";
  std::string outPath = argc > 2 ? argv[2] : "-";

  std::ostringstream inputBuf;
  if (inPath == "-") {
    inputBuf << std::cin.rdbuf();
  } else {
    std::ifstream in(inPath);
    if (!in) {
      std::cout << fail("cannot open input file: " + inPath);
      return 1;
    }
    inputBuf << in.rdbuf();
  }

  json::Value req;
  try {
    req = json::parse(inputBuf.str());
  } catch (const json::ParseError& e) {
    std::cout << fail(std::string("invalid JSON: ") + e.what());
    return 1;
  }
  if (!req.isObject()) {
    std::cout << fail("request root must be a JSON object");
    return 1;
  }

  // ---- points ----
  const json::Value* pts = req.find("points");
  if (!pts || !pts->isArray()) {
    std::cout << fail("missing or invalid 'points' array");
    return 1;
  }
  std::vector<Point> points;
  points.reserve(pts->items.size());
  std::vector<Id> ids;
  ids.reserve(pts->items.size());
  for (std::size_t i = 0; i < pts->items.size(); ++i) {
    const json::Value& pv = pts->items[i];
    const std::string ctx = "points[" + std::to_string(i) + "]: ";
    if (!pv.isObject()) {
      std::cout << fail(ctx + "must be an object");
      return 1;
    }
    const json::Value* idv = pv.find("id");
    const json::Value* xv = pv.find("x");
    const json::Value* yv = pv.find("y");
    if (!idv || !xv || !yv) {
      std::cout << fail(ctx + "each point requires id, x and y");
      return 1;
    }
    Point p;
    std::string err;
    if (!exactInt(*idv, p.id, err)) {
      std::cout << fail(ctx + err);
      return 1;
    }
    if (!finiteNumber(*xv, p.x, err) || !finiteNumber(*yv, p.y, err)) {
      std::cout << fail(ctx + err);
      return 1;
    }
    ids.push_back(p.id);
    points.push_back(p);
  }
  std::sort(ids.begin(), ids.end());
  if (std::adjacent_find(ids.begin(), ids.end()) != ids.end()) {
    std::cout << fail("duplicate point id; ids must be unique (id breaks distance ties)");
    return 1;
  }

  // ---- queries ----
  const json::Value* qs = req.find("queries");
  if (!qs || !qs->isArray()) {
    std::cout << fail("missing or invalid 'queries' array");
    return 1;
  }

  KdTree tree(std::move(points));

  json::Value results = json::Value::makeArray();
  for (std::size_t i = 0; i < qs->items.size(); ++i) {
    const json::Value& qv = qs->items[i];
    const std::string ctx = "queries[" + std::to_string(i) + "]: ";
    if (!qv.isObject()) {
      std::cout << fail(ctx + "must be an object");
      return 1;
    }
    const json::Value* type = qv.find("type");
    const json::Value* x = qv.find("x");
    const json::Value* y = qv.find("y");
    if (!type || !type->is(json::Value::Type::String) || !x || !y) {
      std::cout << fail(ctx + "requires string 'type' and numeric x, y");
      return 1;
    }
    Coord qx, qy;
    std::string err;
    if (!finiteNumber(*x, qx, err) || !finiteNumber(*y, qy, err)) {
      std::cout << fail(ctx + err);
      return 1;
    }

    json::Value item = json::Value::makeObject();
    if (type->text == "knn") {
      const json::Value* kv = qv.find("k");
      std::int64_t k;
      if (!kv || !exactInt(*kv, k, err) || k < 0) {
        std::cout << fail(ctx + "requires non-negative integer 'k'");
        return 1;
      }
      item.members.emplace_back("type", json::Value::makeString("knn"));
      item.members.emplace_back("returned",
                                json::Value::makeInt(static_cast<std::int64_t>(
                                    std::min<std::size_t>(static_cast<std::size_t>(k),
                                                          tree.size()))));
      json::Value arr = json::Value::makeArray();
      for (const auto& n : tree.knn(qx, qy, static_cast<std::size_t>(k)))
        arr.items.push_back(neighborJson(n));
      item.members.emplace_back("neighbors", std::move(arr));
    } else if (type->text == "radius") {
      const json::Value* rv = qv.find("r");
      Coord r;
      if (!rv || !finiteNumber(*rv, r, err) || r < 0) {
        std::cout << fail(ctx + "requires non-negative numeric 'r'");
        return 1;
      }
      item.members.emplace_back("type", json::Value::makeString("radius"));
      json::Value arr = json::Value::makeArray();
      for (const auto& n : tree.radiusSearch(qx, qy, r))
        arr.items.push_back(neighborJson(n));
      item.members.emplace_back("returned",
                                json::Value::makeInt(
                                    static_cast<std::int64_t>(arr.items.size())));
      item.members.emplace_back("neighbors", std::move(arr));
    } else {
      std::cout << fail(ctx + "unknown query type '" + type->text + "'");
      return 1;
    }
    results.items.push_back(std::move(item));
  }

  json::Value meta = json::Value::makeObject();
  meta.members.emplace_back("plane", json::Value::makeString("cartesian_2d"));
  meta.members.emplace_back("distance", json::Value::makeString("euclidean"));
  meta.members.emplace_back("arithmetic",
                            json::Value::makeString("long_double_squared_distances"));
  meta.members.emplace_back("tie_breaker", json::Value::makeString("id_ascending"));
  meta.members.emplace_back("ball", json::Value::makeString("closed_distance_equals_radius_included"));

  json::Value resp = json::Value::makeObject();
  resp.members.emplace_back("ok", json::Value::makeBool(true));
  resp.members.emplace_back("indexed", json::Value::makeInt(static_cast<std::int64_t>(tree.size())));
  resp.members.emplace_back("coordinate_system", std::move(meta));
  resp.members.emplace_back("results", std::move(results));

  const std::string out = json::dump(resp);
  if (outPath == "-") {
    std::cout << out;
  } else {
    std::ofstream of(outPath);
    if (!of) {
      std::cerr << "cannot open output file: " << outPath << "\n";
      return 1;
    }
    of << out;
  }
  return 0;
}
