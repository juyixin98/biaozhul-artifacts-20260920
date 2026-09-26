// main.cpp — JSON request/response CLI for DAG path queries.
//
// Reads exactly one JSON request from stdin, writes one JSON response to
// stdout. Exit code 0 when "ok" is true, 1 otherwise. See README.md for
// the full protocol.
#include <cstdint>
#include <iostream>
#include <iterator>
#include <map>
#include <string>
#include <vector>

#include "dag.hpp"
#include "json.hpp"

namespace {

using Object = std::map<std::string, Json>;

Json errorResponse(const std::string& code, const std::string& message) {
    return Json(Object{
        {"ok", Json(false)},
        {"error", Json(Object{{"code", Json(code)}, {"message", Json(message)}})},
    });
}

const Json& requireField(const Json& obj, const char* name) {
    const Json* v = obj.find(name);
    if (!v) throw DomainError("MISSING_FIELD", std::string("missing field \"") + name + "\"");
    return *v;
}

int64_t requireInt(const Json& v, const char* name) {
    if (!v.isInt()) {
        throw DomainError("TYPE_ERROR",
                          std::string("field \"") + name + "\" must be an integer");
    }
    return v.asInt();
}

int64_t requireIntField(const Json& obj, const char* name) {
    return requireInt(requireField(obj, name), name);
}

Dag buildGraph(const Json& request) {
    const Json& graph = requireField(request, "graph");
    if (!graph.isObject()) {
        throw DomainError("TYPE_ERROR", "field \"graph\" must be an object");
    }
    int64_t numNodes = requireIntField(graph, "num_nodes");
    const Json& edges = requireField(graph, "edges");
    if (!edges.isArray()) {
        throw DomainError("TYPE_ERROR", "field \"graph.edges\" must be an array");
    }
    std::vector<std::pair<int64_t, int64_t>> edgeList;
    edgeList.reserve(edges.asArray().size());
    for (const Json& e : edges.asArray()) {
        if (!e.isArray() || e.asArray().size() != 2) {
            throw DomainError("TYPE_ERROR", "each edge must be a [from,to] pair");
        }
        edgeList.emplace_back(requireInt(e.asArray()[0], "edge.from"),
                              requireInt(e.asArray()[1], "edge.to"));
    }
    return Dag(numNodes, edgeList);
}

BigUint parseRank(const Json& request) {
    const Json& k = requireField(request, "k");
    BigUint rank;
    if (k.isString()) {
        if (!BigUint::fromString(k.asString(), rank)) {
            throw DomainError("TYPE_ERROR",
                              "field \"k\" must be a decimal string of digits");
        }
    } else if (k.isInt()) {
        if (k.asInt() < 0) {
            throw DomainError("TYPE_ERROR", "field \"k\" must be non-negative");
        }
        rank = BigUint(static_cast<uint64_t>(k.asInt()));
    } else {
        throw DomainError("TYPE_ERROR",
                          "field \"k\" must be a decimal string or integer");
    }
    return rank;
}

Json pathToJson(const std::vector<int64_t>& path) {
    std::vector<Json> items;
    items.reserve(path.size());
    for (int64_t v : path) items.emplace_back(v);
    return Json(std::move(items));
}

Json handleCount(const Json& request, const Dag& dag) {
    BigUint count =
        dag.countPaths(requireIntField(request, "source"), requireIntField(request, "target"));
    return Json(Object{
        {"ok", Json(true)},
        {"op", Json(std::string("count"))},
        {"count", Json(count.toString())},
    });
}

Json handleKth(const Json& request, const Dag& dag) {
    BigUint k = parseRank(request);
    std::vector<int64_t> path = dag.kthPath(requireIntField(request, "source"),
                                            requireIntField(request, "target"), k);
    return Json(Object{
        {"ok", Json(true)},
        {"op", Json(std::string("kth"))},
        {"k", Json(k.toString())},
        {"path", pathToJson(path)},
    });
}

Json handleEnumerate(const Json& request, const Dag& dag) {
    int64_t cap = 1000;  // default; see README
    if (const Json* mp = request.find("max_paths")) {
        cap = requireInt(*mp, "max_paths");
    }
    if (cap < 1 || cap > kMaxEnumerateCap) {
        throw DomainError("LIMIT_EXCEEDED",
                          "max_paths must be in [1," + std::to_string(kMaxEnumerateCap) + "]");
    }
    bool truncated = false;
    std::vector<std::vector<int64_t>> paths = dag.enumeratePaths(
        requireIntField(request, "source"), requireIntField(request, "target"), cap, truncated);
    std::vector<Json> pathJsons;
    pathJsons.reserve(paths.size());
    for (const auto& p : paths) pathJsons.push_back(pathToJson(p));
    return Json(Object{
        {"ok", Json(true)},
        {"op", Json(std::string("enumerate"))},
        {"returned", Json(static_cast<int64_t>(paths.size()))},
        {"truncated", Json(truncated)},
        {"paths", Json(std::move(pathJsons))},
    });
}

Json dispatch(const Json& request) {
    if (!request.isObject()) {
        throw DomainError("INVALID_REQUEST", "request must be a JSON object");
    }
    const Json& op = requireField(request, "op");
    if (!op.isString()) {
        throw DomainError("TYPE_ERROR", "field \"op\" must be a string");
    }
    Dag dag = buildGraph(request);
    const std::string& name = op.asString();
    if (name == "count") return handleCount(request, dag);
    if (name == "kth") return handleKth(request, dag);
    if (name == "enumerate") return handleEnumerate(request, dag);
    throw DomainError("UNKNOWN_OP", "unknown op \"" + name +
                                        "\" (expected count|kth|enumerate)");
}

}  // namespace

int main() {
    std::string input((std::istreambuf_iterator<char>(std::cin)),
                      std::istreambuf_iterator<char>());
    Json response = [&]() {
        try {
            return dispatch(Json::parse(input));
        } catch (const JsonError& e) {
            return errorResponse("PARSE_ERROR", e.what());
        } catch (const DomainError& e) {
            return errorResponse(e.code, e.what());
        } catch (const std::exception& e) {
            return errorResponse("INTERNAL_ERROR", e.what());
        }
    }();
    std::cout << response.dump() << '\n';
    const Json* ok = response.find("ok");
    return (ok && ok->isBool() && ok->asBool()) ? 0 : 1;
}
