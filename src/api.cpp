// SPDX-License-Identifier: MIT
#include "api.h"

#include <algorithm>
#include <cctype>
#include <unordered_set>

#include "graph.h"

namespace dagpaths {

namespace {

struct RequestError {
    std::string code;
    std::string message;
};

bool isIntegerLexeme(const std::string& lexeme) {
    size_t i = 0;
    if (!lexeme.empty() && lexeme[0] == '-') i = 1;
    if (i >= lexeme.size()) return false;
    for (; i < lexeme.size(); ++i) {
        if (!std::isdigit(static_cast<unsigned char>(lexeme[i]))) return false;
    }
    return true;
}

long long readInt(const JsonValue& v, const std::string& field) {
    if (!v.isNumber())
        throw RequestError{"INVALID_REQUEST", field + " must be an integer"};
    if (!isIntegerLexeme(v.number))
        throw RequestError{"INVALID_REQUEST",
                           field + " must be an integer without fraction or exponent"};
    try {
        return std::stoll(v.number);
    } catch (const std::exception&) {
        throw RequestError{"INVALID_REQUEST", field + " is out of integer range"};
    }
}

const JsonValue* requireField(const JsonValue& req, const std::string& key) {
    const JsonValue* v = req.get(key);
    if (v == nullptr)
        throw RequestError{"INVALID_REQUEST", "missing required field: " + key};
    return v;
}

int readNode(const JsonValue& v, int nodeCount, const std::string& field) {
    const long long id = readInt(v, field);
    if (id < 0 || id >= nodeCount)
        throw RequestError{"INVALID_REQUEST",
                           field + " refers to node " + std::to_string(id)
                           + ", valid range is 0.." + std::to_string(nodeCount - 1)};
    return static_cast<int>(id);
}

// Accepts either a scalar integer or an array of integers.
std::vector<int> readNodes(const JsonValue& v, int nodeCount,
                           const std::string& field, bool& batch) {
    if (v.isArray()) {
        if (v.arr.empty())
            throw RequestError{"INVALID_REQUEST", field + " list must not be empty"};
        batch = true;
        std::vector<int> out;
        out.reserve(v.arr.size());
        for (const JsonValue& item : v.arr)
            out.push_back(readNode(item, nodeCount, field + "[]"));
        return out;
    }
    batch = false;
    return {readNode(v, nodeCount, field)};
}

Graph parseGraph(const JsonValue& req) {
    const JsonValue* g = requireField(req, "graph");
    if (!g->isObject())
        throw RequestError{"INVALID_REQUEST", "graph must be an object"};
    const long long nodeCount = readInt(*requireField(*g, "nodes"), "graph.nodes");
    if (nodeCount < 1)
        throw RequestError{"INVALID_GRAPH", "graph.nodes must be >= 1"};
    const JsonValue* edges = requireField(*g, "edges");
    if (!edges->isArray())
        throw RequestError{"INVALID_REQUEST", "graph.edges must be an array"};

    std::vector<Edge> edgeList;
    edgeList.reserve(edges->arr.size());
    for (size_t i = 0; i < edges->arr.size(); ++i) {
        const JsonValue& e = edges->arr[i];
        const std::string ctx = "graph.edges[" + std::to_string(i) + "]";
        if (!e.isArray() || e.arr.size() != 2)
            throw RequestError{"INVALID_REQUEST", ctx + " must be a [from, to] pair"};
        const long long u = readInt(e.arr[0], ctx + "[0]");
        const long long v = readInt(e.arr[1], ctx + "[1]");
        if (u < 0 || u >= nodeCount || v < 0 || v >= nodeCount)
            throw RequestError{"INVALID_GRAPH", ctx + " endpoint out of range"};
        edgeList.emplace_back(static_cast<int>(u), static_cast<int>(v));
    }
    try {
        return Graph::build(static_cast<int>(nodeCount), std::move(edgeList));
    } catch (const std::invalid_argument& e) {
        throw RequestError{"INVALID_GRAPH", e.what()};
    }
}

BigInt readK(const JsonValue& v) {
    std::string digits;
    if (v.isString()) {
        digits = v.str;
    } else if (v.isNumber()) {
        if (!isIntegerLexeme(v.number))
            throw RequestError{"INVALID_REQUEST",
                               "k must be a non-negative decimal integer (no fraction/exponent); "
                               "send it as a string for very large values"};
        digits = v.number;
    } else {
        throw RequestError{"INVALID_REQUEST", "k must be an integer or decimal string"};
    }
    if (!digits.empty() && digits[0] == '-')
        throw RequestError{"INVALID_REQUEST", "k must be >= 1"};
    if (digits.empty() || !std::all_of(digits.begin(), digits.end(),
            [](char c) { return std::isdigit(static_cast<unsigned char>(c)); }))
        throw RequestError{"INVALID_REQUEST", "k must contain decimal digits only"};
    if (digits.size() > MAX_DIGITS_K)
        throw RequestError{"INVALID_REQUEST",
                           "k exceeds the supported precision of "
                           + std::to_string(MAX_DIGITS_K) + " decimal digits"};
    return BigInt::parseDecimal(digits);
}

JsonValue pathArray(const std::vector<int>& path) {
    JsonValue arr;
    arr.type = JsonType::Array;
    for (int node : path)
        arr.arr.push_back(JsonValue::makeNumber(std::to_string(node)));
    return arr;
}

JsonValue pairResult(int s, int t, const BigInt& total) {
    JsonValue row;
    row.type = JsonType::Object;
    row.obj.emplace("source", JsonValue::makeNumber(std::to_string(s)));
    row.obj.emplace("target", JsonValue::makeNumber(std::to_string(t)));
    row.obj.emplace("paths", JsonValue::makeString(total.str()));
    return row;
}

JsonValue handleCount(const JsonValue& req, const Graph& graph) {
    bool sourceBatch = false, targetBatch = false;
    std::vector<int> sources =
        readNodes(*requireField(req, "source"), graph.nodeCount(), "source", sourceBatch);
    std::vector<int> targets =
        readNodes(*requireField(req, "target"), graph.nodeCount(), "target", targetBatch);
    if (static_cast<std::size_t>(sources.size()) * targets.size() > MAX_PAIRS)
        throw RequestError{"INVALID_REQUEST",
                           "source x target pair count exceeds limit of "
                           + std::to_string(MAX_PAIRS)};

    if (!sourceBatch && !targetBatch)
        return pairResult(sources[0], targets[0],
                          graph.countPaths(sources[0], targets[0]));

    JsonValue rows;
    rows.type = JsonType::Array;
    for (int s : sources)
        for (int t : targets)
            rows.arr.push_back(pairResult(s, t, graph.countPaths(s, t)));
    JsonValue data;
    data.type = JsonType::Object;
    data.obj.emplace("results", std::move(rows));
    return data;
}

JsonValue handleKth(const JsonValue& req, const Graph& graph) {
    const int s = readNode(*requireField(req, "source"), graph.nodeCount(), "source");
    const int t = readNode(*requireField(req, "target"), graph.nodeCount(), "target");
    const BigInt k = readK(*requireField(req, "k"));
    if (k < BigInt::one())
        throw RequestError{"INVALID_REQUEST", "k must be >= 1"};

    std::vector<int> path;
    try {
        path = graph.kthPath(s, t, k);
    } catch (const std::out_of_range&) {
        // Covers both unreachable pairs (total 0) and k past the end.
        throw RequestError{"K_OUT_OF_RANGE",
                           "k=" + k.str() + " is larger than the number of paths ("
                           + graph.countPaths(s, t).str() + ") from "
                           + std::to_string(s) + " to " + std::to_string(t)};
    }
    JsonValue data;
    data.type = JsonType::Object;
    data.obj.emplace("source", JsonValue::makeNumber(std::to_string(s)));
    data.obj.emplace("target", JsonValue::makeNumber(std::to_string(t)));
    data.obj.emplace("k", JsonValue::makeString(k.str()));
    data.obj.emplace("total", JsonValue::makeString(graph.countPaths(s, t).str()));
    data.obj.emplace("path", pathArray(path));
    return data;
}

JsonValue handleRank(const JsonValue& req, const Graph& graph) {
    const JsonValue* pathJson = requireField(req, "path");
    if (!pathJson->isArray() || pathJson->arr.empty())
        throw RequestError{"INVALID_REQUEST", "path must be a non-empty array of node ids"};
    std::vector<int> path;
    path.reserve(pathJson->arr.size());
    for (const JsonValue& node : pathJson->arr)
        path.push_back(readNode(node, graph.nodeCount(), "path[]"));

    BigInt rank;
    try {
        rank = graph.rankOfPath(path);
    } catch (const std::invalid_argument& e) {
        throw RequestError{"INVALID_PATH", e.what()};
    }
    JsonValue data;
    data.type = JsonType::Object;
    data.obj.emplace("path", pathArray(path));
    data.obj.emplace("rank", JsonValue::makeString(rank.str()));
    data.obj.emplace("total",
                     JsonValue::makeString(graph.countPaths(path.front(), path.back()).str()));
    return data;
}

JsonValue handleEnumerate(const JsonValue& req, const Graph& graph) {
    const int s = readNode(*requireField(req, "source"), graph.nodeCount(), "source");
    const int t = readNode(*requireField(req, "target"), graph.nodeCount(), "target");

    std::vector<std::vector<int>> paths;
    try {
        paths = graph.enumeratePaths(s, t);
    } catch (const std::length_error& e) {
        throw RequestError{"ENUMERATION_LIMIT", e.what()};
    }
    JsonValue arr;
    arr.type = JsonType::Array;
    for (const auto& path : paths) arr.arr.push_back(pathArray(path));

    JsonValue data;
    data.type = JsonType::Object;
    data.obj.emplace("source", JsonValue::makeNumber(std::to_string(s)));
    data.obj.emplace("target", JsonValue::makeNumber(std::to_string(t)));
    data.obj.emplace("count", JsonValue::makeString(std::to_string(paths.size())));
    data.obj.emplace("paths", std::move(arr));
    return data;
}

} // namespace

JsonValue errorResponse(const std::string& code, const std::string& message) {
    JsonValue error;
    error.type = JsonType::Object;
    error.obj.emplace("code", JsonValue::makeString(code));
    error.obj.emplace("message", JsonValue::makeString(message));
    JsonValue root;
    root.type = JsonType::Object;
    root.obj.emplace("ok", JsonValue::makeBool(false));
    root.obj.emplace("error", std::move(error));
    return root;
}

JsonValue handleRequest(const JsonValue& request) {
    try {
        if (!request.isObject())
            return errorResponse("INVALID_REQUEST", "request must be a JSON object");
        const JsonValue* action = request.get("action");
        if (action == nullptr || !action->isString())
            return errorResponse("INVALID_REQUEST",
                                 "missing or invalid 'action'; expected one of "
                                 "count, kth, rank, enumerate");
        const std::string& name = action->str;

        Graph graph = parseGraph(request);
        JsonValue data;
        if (name == "count") data = handleCount(request, graph);
        else if (name == "kth") data = handleKth(request, graph);
        else if (name == "rank") data = handleRank(request, graph);
        else if (name == "enumerate") data = handleEnumerate(request, graph);
        else
            return errorResponse("INVALID_REQUEST", "unknown action: " + name);

        JsonValue root;
        root.type = JsonType::Object;
        root.obj.emplace("ok", JsonValue::makeBool(true));
        root.obj.emplace("data", std::move(data));
        return root;
    } catch (const RequestError& e) {
        return errorResponse(e.code, e.message);
    } catch (const std::exception& e) {
        return errorResponse("INTERNAL_ERROR", e.what());
    }
}

} // namespace dagpaths
