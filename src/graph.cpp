#include "graph.hpp"

#include <algorithm>
#include <unordered_map>

namespace {

struct Builder {
    std::vector<std::string> labels;
    std::unordered_map<std::string, int> id;

    int intern(const std::string& key) {
        auto it = id.find(key);
        if (it != id.end()) return it->second;
        int v = static_cast<int>(labels.size());
        id.emplace(key, v);
        labels.push_back(key);
        return v;
    }

    static std::string endpointKey(const json::Value& ep) {
        if (ep.isString()) return ep.asString();
        if (ep.isInt()) return std::to_string(ep.asInt());
        if (ep.isNumber()) {
            // Reject fractional / absurdly large endpoints.
            double d = ep.asDouble();
            if (d != static_cast<double>(static_cast<long long>(d)))
                throw std::runtime_error("graph: non-integer vertex id");
            return std::to_string(static_cast<long long>(d));
        }
        throw std::runtime_error("graph: edge endpoint must be a string or integer");
    }
};

void addEdge(Graph& g, int u, int v) {
    if (u == v) throw std::runtime_error("graph: self-loops are not supported");
    g.adj[u][v] = g.adj[v][u] = 1;
}

}  // namespace

Graph graphFromJson(const json::Value& spec) {
    if (!spec.isObject())
        throw std::runtime_error("graph: expected a JSON object");

    Builder b;

    if (spec.contains("vertices")) {
        const auto& verts = spec["vertices"];
        if (!verts.isArray())
            throw std::runtime_error("graph: 'vertices' must be an array");
        for (const auto& v : verts.asArray()) {
            if (!v.isString() && !v.isInt())
                throw std::runtime_error(
                    "graph: 'vertices' entries must be strings or integers");
            std::string key = Builder::endpointKey(v);
            if (b.id.count(key))
                throw std::runtime_error("graph: duplicate vertex: " + key);
            b.intern(key);
        }
    }

    Graph g;

    if (spec.contains("n")) {
        if (!spec["n"].isInt() || spec["n"].asInt() < 0)
            throw std::runtime_error("graph: 'n' must be a non-negative integer");
        int n = static_cast<int>(spec["n"].asInt());
        for (int i = 0; i < n; ++i) {
            std::string key = std::to_string(i);
            if (!b.id.count(key)) b.intern(key);
        }
    }

    auto internEp = [&](const json::Value& ep) {
        return b.intern(Builder::endpointKey(ep));
    };

    if (spec.contains("edges")) {
        const auto& edges = spec["edges"];
        if (!edges.isArray())
            throw std::runtime_error("graph: 'edges' must be an array");
        for (const auto& e : edges.asArray()) {
            if (!e.isArray() || e.size() != 2)
                throw std::runtime_error(
                    "graph: each edge must be a [u, v] pair");
            internEp(e.at(0));
            internEp(e.at(1));
        }
    }

    if (spec.contains("adjacency")) {
        const auto& adj = spec["adjacency"];
        if (!adj.isObject())
            throw std::runtime_error("graph: 'adjacency' must be an object");
        for (const auto& [k, nbrs] : adj.asObject()) {
            b.intern(k);
            if (!nbrs.isArray())
                throw std::runtime_error(
                    "graph: adjacency lists must be arrays");
            for (const auto& w : nbrs.asArray()) b.intern(Builder::endpointKey(w));
        }
    }

    g.labels = std::move(b.labels);
    int n = static_cast<int>(g.labels.size());
    g.adj.assign(n, std::vector<char>(n, 0));

    if (spec.contains("edges")) {
        for (const auto& e : spec["edges"].asArray()) {
            int u = b.id.at(Builder::endpointKey(e.at(0)));
            int v = b.id.at(Builder::endpointKey(e.at(1)));
            addEdge(g, u, v);
        }
    }

    if (spec.contains("adjacency")) {
        for (const auto& [k, nbrs] : spec["adjacency"].asObject()) {
            int u = b.id.at(k);
            for (const auto& w : nbrs.asArray()) {
                int v = b.id.at(Builder::endpointKey(w));
                addEdge(g, u, v);
            }
        }
    }

    if (!spec.contains("vertices") && !spec.contains("edges") &&
        !spec.contains("adjacency") && !spec.contains("n")) {
        throw std::runtime_error(
            "graph: expected 'vertices'/'edges', 'n'/'edges', or 'adjacency'");
    }
    return g;
}

json::Value graphToJson(const Graph& g) {
    json::Value::ArrayT verts;
    verts.reserve(g.n());
    for (const auto& l : g.labels) verts.push_back(json::Value(l));

    json::Value::ArrayT edges;
    for (int u = 0; u < g.n(); ++u) {
        for (int v = u + 1; v < g.n(); ++v) {
            if (g.adj[u][v]) {
                edges.push_back(json::Value::ArrayT{
                    json::Value(g.labels[u]), json::Value(g.labels[v])});
            }
        }
    }
    // Built incrementally (rather than via nested brace-init) to stay
    // warning-clean under GCC's variant/map inlining analysis.
    json::Value::ObjectT out;
    out.emplace("vertices", json::Value(std::move(verts)));
    out.emplace("edges", json::Value(std::move(edges)));
    return json::Value(std::move(out));
}
