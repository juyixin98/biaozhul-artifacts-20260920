#include "graph_builder.hpp"

#include <algorithm>
#include <unordered_map>

namespace {

[[noreturn]] void badRequest(const std::string& msg) {
    throw std::invalid_argument(msg);
}

const json::Value& edgeEndpoint(const json::Value& edge, std::size_t edgeIndex, const char* key) {
    if (edge.type != json::Value::Object)
        badRequest("edge at index " + std::to_string(edgeIndex) + " must be an object");
    if (!edge.has(key))
        badRequest("edge at index " + std::to_string(edgeIndex) + " requires '" + key + "'");
    const json::Value& ep = edge.at(key);
    if (ep.type != json::Value::Number && ep.type != json::Value::String)
        badRequest("edge '" + std::string(key) + "' at index " + std::to_string(edgeIndex) +
                   " must be an integer index or a string label");
    return ep;
}

} // namespace

Graph buildGraph(const json::Value& request) {
    if (request.type != json::Value::Object) badRequest("request body must be a JSON object");

    Graph g;
    std::unordered_map<std::string, int> labelIndex;
    bool nFromVertices = false;

    if (request.has("vertices")) {
        const json::Value& verts = request.at("vertices");
        if (verts.type == json::Value::Number) {
            std::int64_t cnt = verts.asInt();
            if (cnt <= 0) badRequest("vertex count must be positive");
            if (cnt > limits::MAX_N)
                badRequest("vertex count " + std::to_string(cnt) + " exceeds limit " +
                           std::to_string(limits::MAX_N));
            g.n = static_cast<int>(cnt);
        } else if (verts.type == json::Value::Array) {
            if (verts.arr.empty()) badRequest("vertex list must not be empty");
            if (static_cast<std::int64_t>(verts.arr.size()) > limits::MAX_N)
                badRequest("too many vertices (limit " + std::to_string(limits::MAX_N) + ")");
            g.n = static_cast<int>(verts.arr.size());
            g.labels.reserve(g.n);
            for (std::size_t i = 0; i < verts.arr.size(); ++i) {
                if (verts.arr[i].type != json::Value::String)
                    badRequest("vertex labels must be JSON strings (entry " + std::to_string(i) + ")");
                const std::string& label = verts.arr[i].str;
                if (!labelIndex.emplace(label, static_cast<int>(i)).second)
                    badRequest("duplicate vertex label '" + label + "'");
                g.labels.push_back(label);
            }
        } else {
            badRequest("'vertices' must be a non-empty string array or a positive integer count");
        }
        nFromVertices = true;
    }

    if (!request.has("edges")) badRequest("request is missing required 'edges' array");
    const json::Value& edges = request.at("edges");
    if (edges.type != json::Value::Array) badRequest("'edges' must be an array");
    if (static_cast<std::int64_t>(edges.arr.size()) > limits::MAX_RAW_EDGES)
        badRequest("too many raw edges: " + std::to_string(edges.arr.size()) + " (limit " +
                   std::to_string(limits::MAX_RAW_EDGES) + ")");
    g.totalRawEdges = static_cast<std::int64_t>(edges.arr.size());

    // Pass 1: validate shapes and (if needed) derive n from integer endpoints.
    int derivedN = 0;
    for (std::size_t i = 0; i < edges.arr.size(); ++i) {
        const json::Value& f = edgeEndpoint(edges.arr[i], i, "from");
        const json::Value& t = edgeEndpoint(edges.arr[i], i, "to");
        if (f.type == json::Value::String || t.type == json::Value::String) {
            if (!nFromVertices)
                badRequest("string edge endpoint at index " + std::to_string(i) +
                           " requires an explicit 'vertices' label list");
        }
        if (f.type == json::Value::Number) {
            std::int64_t v = f.asInt();
            if (v < 0) badRequest("negative edge index at edge " + std::to_string(i));
            derivedN = std::max(derivedN, static_cast<int>(v) + 1);
        }
        if (t.type == json::Value::Number) {
            std::int64_t v = t.asInt();
            if (v < 0) badRequest("negative edge index at edge " + std::to_string(i));
            derivedN = std::max(derivedN, static_cast<int>(v) + 1);
        }
    }

    if (!nFromVertices) {
        if (derivedN == 0)
            badRequest("cannot derive vertex count: provide 'vertices' or at least one edge");
        if (derivedN > limits::MAX_N)
            badRequest("derived vertex count " + std::to_string(derivedN) + " exceeds limit " +
                       std::to_string(limits::MAX_N));
        g.n = derivedN;
    }
    if (g.n == 0) g.n = 1; // vertices given but no edges: keep a valid nonempty graph

    if (g.labels.empty()) {
        g.labels.reserve(g.n);
        for (int i = 0; i < g.n; ++i) g.labels.push_back(std::to_string(i));
    }
    if (labelIndex.empty()) {
        for (int i = 0; i < g.n; ++i) labelIndex.emplace(g.labels[static_cast<std::size_t>(i)], i);
    }

    // Pass 2: resolve endpoints directly into the compact sorted-edge vector.
    auto resolve = [&](const json::Value& ep, std::size_t edgeIndex, const char* side) -> int {
        if (ep.type == json::Value::Number) {
            int idx = static_cast<int>(ep.asInt());
            if (idx >= g.n)
                badRequest("edge " + std::to_string(edgeIndex) + " " + side + "-index " +
                           std::to_string(idx) + " out of range [0," + std::to_string(g.n - 1) + "]");
            return idx;
        }
        auto it = labelIndex.find(ep.str);
        if (it == labelIndex.end())
            badRequest("edge " + std::to_string(edgeIndex) + " references unknown vertex label '" +
                       ep.str + "'");
        return it->second;
    };

    std::vector<std::pair<int, int>> pairs;
    pairs.reserve(edges.arr.size());
    for (std::size_t i = 0; i < edges.arr.size(); ++i)
        pairs.emplace_back(resolve(edges.arr[i].at("from"), i, "from"),
                           resolve(edges.arr[i].at("to"), i, "to"));

    // Aggregate identical (from,to) pairs into multiplicities.
    std::sort(pairs.begin(), pairs.end());
    g.adj.assign(g.n, {});
    g.radj.assign(g.n, {});
    for (std::size_t i = 0; i < pairs.size();) {
        std::size_t j = i + 1;
        while (j < pairs.size() && pairs[j] == pairs[i]) ++j;
        std::int64_t mult = static_cast<std::int64_t>(j - i);
        int u = pairs[i].first, v = pairs[i].second;
        g.uniqueEdges.push_back({u, v, mult});
        g.adj[u].emplace_back(v, mult);
        g.radj[v].emplace_back(u, mult);
        i = j;
    }
    for (auto& row : g.adj) std::sort(row.begin(), row.end());
    for (auto& row : g.radj) std::sort(row.begin(), row.end());
    return g;
}
