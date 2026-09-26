#include "graph.hpp"

#include <set>

namespace tdw {

Graph parseGraph(const Json& req) {
    const Json* jn = req.find("num_vertices");
    if (jn == nullptr) jn = req.find("n");
    if (jn == nullptr) throw InputError("missing 'num_vertices'");
    long long nll = 0;
    if (!jn->isIntegral(nll) || nll < 0 || nll > MAX_VERTICES) {
        throw InputError("'num_vertices' must be an integer in [0, " +
                         std::to_string(MAX_VERTICES) + "]");
    }
    Graph g;
    g.n = static_cast<int>(nll);
    g.adj.assign(g.n, 0);

    const Json* je = req.find("edges");
    if (je != nullptr) {
        if (je->type != Json::ARR) throw InputError("'edges' must be an array");
        std::set<std::pair<int, int>> seen;
        for (const Json& e : je->items) {
            if (e.type != Json::ARR || e.items.size() != 2) {
                throw InputError("each edge must be [u, v]");
            }
            long long u = 0, v = 0;
            if (!e.items[0].isIntegral(u) || !e.items[1].isIntegral(v)) {
                throw InputError("edge endpoints must be integers");
            }
            if (u < 0 || u >= g.n || v < 0 || v >= g.n) {
                throw InputError("edge endpoint out of range: [" +
                                 std::to_string(u) + ", " + std::to_string(v) + "]");
            }
            if (u == v) throw InputError("self loops are not allowed");
            if (!seen.insert({std::min<int>(int(u), int(v)),
                              std::max<int>(int(u), int(v))}).second) {
                throw InputError("duplicate edge: [" + std::to_string(u) +
                                 ", " + std::to_string(v) + "]");
            }
            g.adj[u] |= 1ULL << v;
            g.adj[v] |= 1ULL << u;
        }
    }
    return g;
}

Json graphToJson(const Graph& g) {
    Json je;
    je.type = Json::ARR;
    for (int u = 0; u < g.n; ++u) {
        uint64_t bits = g.adj[u];
        while (bits) {
            int v = __builtin_ctzll(bits);
            if (u < v) {
                Json edge;
                edge.type = Json::ARR;
                Json a; a.type = Json::NUM; a.number = u;
                Json b; b.type = Json::NUM; b.number = v;
                edge.items = {a, b};
                je.items.push_back(std::move(edge));
            }
            bits &= bits - 1;
        }
    }
    Json jn;
    jn.type = Json::NUM;
    jn.number = g.n;
    Json out;
    out.type = Json::OBJ;
    out.members = {{"num_vertices", jn}, {"edges", std::move(je)}};
    return out;
}

} // namespace tdw
