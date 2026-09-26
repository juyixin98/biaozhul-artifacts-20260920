#include "validator.hpp"

#include <algorithm>
#include <cstdint>
#include <map>
#include <set>
#include <utility>
#include <vector>

namespace tdw {

namespace {

Json num(long long v) {
    Json j; j.type = Json::NUM; j.number = static_cast<double>(v); return j;
}
Json boolean(bool b) {
    Json j; j.type = Json::BOOL; j.boolean = b; return j;
}
Json str(const std::string& s) {
    Json j; j.type = Json::STR; j.text = s; return j;
}

bool asInt(const Json* j, long long& out) {
    return j != nullptr && j->isIntegral(out);
}

// Independent replay (own adjacency copy, own loops) returning the later
// neighbors per elimination step and the fill set.
struct IndependentReplay {
    int width = 0;
    std::vector<std::vector<int>> laterNeighbors;
    std::set<std::pair<int, int>> fillEdges;
};

IndependentReplay independentlyReplay(int n,
                                      const std::set<std::pair<int, int>>& edges,
                                      const std::vector<int>& order) {
    IndependentReplay r;
    std::vector<uint64_t> adj(n, 0);
    for (auto [u, v] : edges) {
        adj[u] |= 1ULL << v;
        adj[v] |= 1ULL << u;
    }
    uint64_t alive = n == 64 ? ~0ULL : (n == 0 ? 0ULL : (1ULL << n) - 1);
    r.laterNeighbors.resize(n);
    for (int v : order) {
        uint64_t nb = adj[v] & alive;
        std::vector<int> nbs;
        uint64_t row = nb;
        while (row) {
            int u = __builtin_ctzll(row);
            nbs.push_back(u);
            r.laterNeighbors[v].push_back(u);
            row &= row - 1;
        }
        r.width = std::max(r.width, static_cast<int>(nbs.size()));
        for (size_t i = 0; i < nbs.size(); ++i) {
            for (size_t j = i + 1; j < nbs.size(); ++j) {
                int u = nbs[i], w = nbs[j];
                if (!edges.count({std::min(u, w), std::max(u, w)})) {
                    r.fillEdges.insert({std::min(u, w), std::max(u, w)});
                }
                adj[u] |= 1ULL << w;
                adj[w] |= 1ULL << u;
            }
        }
        alive &= ~(1ULL << v);
    }
    return r;
}

} // namespace

Json ValidationReport::toJson() const {
    Json o;
    o.type = Json::OBJ;
    o.members = {
        {"valid", boolean(valid)},
        {"errors", [&] {
            Json a; a.type = Json::ARR;
            for (const auto& e : errors) a.items.push_back(str(e));
            return a;
        }()},
        {"vertex_count", num(vertexCount)},
        {"edge_count", num(edgeCount)},
        {"bag_count", num(bagCount)},
        {"bag_tree_edge_count", num(bagTreeEdgeCount)},
        {"uncovered_edges", num(uncoveredEdges)},
        {"vertices_missing_from_bags", num(verticesMissingFromBags)},
        {"running_intersection_violations", num(runningIntersectionViolations)},
        {"reported_width", num(reportedWidth)},
        {"recomputed_width", num(recomputedWidth)},
        {"max_bag_size", num(maxBagSize)},
        {"replay_width_matches", boolean(replayWidthMatches)},
        {"fill_edges_match_replay", boolean(fillEdgesMatchReplay)},
        {"width_labeled_non_optimal", boolean(widthLabeledNonOptimal)}
    };
    return o;
}

ValidationReport validateResponse(const Json& resp) {
    ValidationReport rep;
    auto err = [&](const std::string& m) { rep.errors.push_back(m); };

    const Json* g = resp.find("graph");
    const Json* orderJ = resp.find("elimination_order");
    const Json* td = resp.find("tree_decomposition");
    if (g == nullptr || orderJ == nullptr || td == nullptr) {
        err("response missing graph / elimination_order / tree_decomposition");
        return rep;
    }

    long long nll = 0;
    if (!asInt(g->find("num_vertices"), nll) || nll < 0 || nll > 64) {
        err("invalid graph.num_vertices");
        return rep;
    }
    int n = static_cast<int>(nll);
    rep.vertexCount = n;

    const Json* edgesJ = g->find("edges");
    if (edgesJ == nullptr || edgesJ->type != Json::ARR) {
        err("graph.edges missing or not an array");
        return rep;
    }
    std::set<std::pair<int, int>> edges;
    for (const Json& e : edgesJ->items) {
        if (e.type != Json::ARR || e.items.size() != 2) {
            err("edge is not a pair"); return rep;
        }
        long long u, v;
        if (!e.items[0].isIntegral(u) || !e.items[1].isIntegral(v) ||
            u < 0 || u >= n || v < 0 || v >= n || u == v) {
            err("edge endpoint invalid"); return rep;
        }
        edges.insert({static_cast<int>(std::min(u, v)),
                      static_cast<int>(std::max(u, v))});
    }
    rep.edgeCount = static_cast<int>(edges.size());

    // ---- Order is a permutation ----------------------------------------
    if (orderJ->type != Json::ARR || static_cast<int>(orderJ->items.size()) != n) {
        err("elimination_order must have exactly n entries");
        return rep;
    }
    std::vector<int> order(n);
    std::vector<char> used(n, 0);
    for (int i = 0; i < n; ++i) {
        long long v;
        if (!orderJ->items[i].isIntegral(v) || v < 0 || v >= n || used[v]) {
            err("elimination_order is not a permutation of 0..n-1");
            return rep;
        }
        used[v] = 1;
        order[i] = static_cast<int>(v);
    }

    // ---- Bags ------------------------------------------------------------
    const Json* bagsJ = td->find("bags");
    const Json* bteJ = td->find("bag_tree_edges");
    if (bagsJ == nullptr || bagsJ->type != Json::ARR ||
        bteJ == nullptr || bteJ->type != Json::ARR) {
        err("tree_decomposition.bags / bag_tree_edges missing or malformed");
        return rep;
    }
    if (static_cast<int>(bagsJ->items.size()) != n) {
        err("number of bags must equal number of vertices");
        return rep;
    }
    std::vector<std::set<int>> bagSets(n);
    rep.maxBagSize = 0;
    for (int i = 0; i < n; ++i) {
        const Json& b = bagsJ->items[i];
        const Json* verts = b.type == Json::OBJ ? b.find("vertices") : &b;
        if (verts == nullptr || verts->type != Json::ARR) {
            err("bag vertices missing"); return rep;
        }
        for (const Json& x : verts->items) {
            long long v;
            if (!x.isIntegral(v) || v < 0 || v >= n) {
                err("bag contains invalid vertex"); return rep;
            }
            bagSets[i].insert(static_cast<int>(v));
        }
        rep.maxBagSize = std::max(rep.maxBagSize,
                                  static_cast<int>(bagSets[i].size()));
    }

    // Vertex coverage.
    std::vector<char> covered(n, 0);
    for (const auto& bag : bagSets)
        for (int v : bag) covered[v] = 1;
    rep.verticesMissingFromBags =
        static_cast<int>(std::count(covered.begin(), covered.end(), 0));
    if (rep.verticesMissingFromBags) err("some vertices appear in no bag");

    // Edge coverage (original graph edges and reported fill edges).
    auto coveredByBag = [&](int u, int v) {
        for (const auto& bag : bagSets)
            if (bag.count(u) && bag.count(v)) return true;
        return false;
    };
    for (auto [u, v] : edges) {
        if (!coveredByBag(u, v)) {
            ++rep.uncoveredEdges;
            err("graph edge {" + std::to_string(u) + "," +
                std::to_string(v) + "} is in no bag");
        }
    }

    // ---- Bag tree --------------------------------------------------------
    std::set<std::pair<int, int>> bteSet;
    for (const Json& e : bteJ->items) {
        if (e.type != Json::ARR || e.items.size() != 2) {
            err("bag tree edge is not a pair"); return rep;
        }
        long long a, b;
        if (!e.items[0].isIntegral(a) || !e.items[1].isIntegral(b) ||
            a < 0 || a >= n || b < 0 || b >= n || a == b) {
            err("bag tree edge endpoints invalid"); return rep;
        }
        bteSet.insert({static_cast<int>(std::min(a, b)),
                       static_cast<int>(std::max(a, b))});
    }
    rep.bagTreeEdgeCount = static_cast<int>(bteSet.size());
    rep.bagCount = n;
    if (n > 0 && rep.bagTreeEdgeCount != n - 1) {
        err("bag graph must have exactly n-1 edges (got " +
            std::to_string(rep.bagTreeEdgeCount) + ")");
    }
    std::vector<std::vector<int>> adjBag(n);
    for (auto [a, b] : bteSet) {
        adjBag[a].push_back(b);
        adjBag[b].push_back(a);
    }
    if (n > 0) {
        std::vector<char> seen(n, 0);
        std::vector<int> st{0};
        seen[0] = 1;
        while (!st.empty()) {
            int x = st.back(); st.pop_back();
            for (int y : adjBag[x]) if (!seen[y]) { seen[y] = 1; st.push_back(y); }
        }
        if (std::count(seen.begin(), seen.end(), 0) > 0) {
            err("bag graph is not connected");
        }
    }

    // ---- Running intersection -------------------------------------------
    for (int v = 0; v < n; ++v) {
        std::vector<int> hosts;
        for (int i = 0; i < n; ++i)
            if (bagSets[i].count(v)) hosts.push_back(i);
        if (hosts.empty()) continue; // already reported above
        std::set<int> hostSet(hosts.begin(), hosts.end());
        std::set<int> reached;
        std::vector<int> st{hosts[0]};
        reached.insert(hosts[0]);
        while (!st.empty()) {
            int x = st.back(); st.pop_back();
            for (int y : adjBag[x]) {
                if (hostSet.count(y) && !reached.count(y)) {
                    reached.insert(y);
                    st.push_back(y);
                }
            }
        }
        if (reached != hostSet) {
            ++rep.runningIntersectionViolations;
            err("running intersection violated for vertex " + std::to_string(v));
        }
    }

    // ---- Independent replay ---------------------------------------------
    IndependentReplay replay = independentlyReplay(n, edges, order);
    rep.recomputedWidth = replay.width;

    long long reported = -1;
    const Json* hw = resp.find("heuristic_width");
    if (hw == nullptr) hw = resp.find("width");
    if (asInt(hw, reported)) rep.reportedWidth = static_cast<int>(reported);
    rep.replayWidthMatches = (rep.reportedWidth == replay.width);
    if (!rep.replayWidthMatches) {
        err("reported width " + std::to_string(rep.reportedWidth) +
            " != independently replayed width " +
            std::to_string(replay.width));
    }

    long long tdwVal = -1;
    if (asInt(td->find("width"), tdwVal) &&
        static_cast<int>(tdwVal) != replay.width) {
        err("tree_decomposition.width disagrees with replay");
    }
    if (n > 0 && rep.maxBagSize - 1 != replay.width) {
        err("max bag size - 1 disagrees with replay width");
    }

    // Every bag must equal {order[i]} union later-neighbors (elimination bag).
    for (int i = 0; i < n; ++i) {
        std::set<int> expect(replay.laterNeighbors[order[i]].begin(),
                             replay.laterNeighbors[order[i]].end());
        expect.insert(order[i]);
        if (expect != bagSets[i]) {
            err("bag " + std::to_string(i) +
                " is not the elimination bag of order[" +
                std::to_string(i) + "]");
        }
    }

    // Fill edges reported must match the independent replay exactly.
    const Json* fillJ = resp.find("fill_edges");
    if (fillJ != nullptr) {
        if (fillJ->type != Json::ARR) {
            err("fill_edges is not an array");
        } else {
            std::set<std::pair<int, int>> reportedFill;
            for (const Json& e : fillJ->items) {
                long long u, v;
                if (e.type != Json::ARR || e.items.size() != 2 ||
                    !e.items[0].isIntegral(u) || !e.items[1].isIntegral(v)) {
                    err("fill edge malformed"); continue;
                }
                reportedFill.insert({static_cast<int>(std::min(u, v)),
                                     static_cast<int>(std::max(u, v))});
            }
            rep.fillEdgesMatchReplay = (reportedFill == replay.fillEdges);
            if (!rep.fillEdgesMatchReplay) err("fill_edges mismatch replay");
            // Fill edges must also be covered by some bag.
            for (auto [u, v] : replay.fillEdges) {
                if (!coveredByBag(u, v)) {
                    ++rep.uncoveredEdges;
                    err("fill edge {" + std::to_string(u) + "," +
                        std::to_string(v) + "} is in no bag");
                }
            }
        }
    }

    // ---- Honest labeling -------------------------------------------------
    const Json* isOpt = resp.find("is_optimal");
    const Json* claim = resp.find("width_claim");
    std::string claimText = claim != nullptr ? claim->text : std::string();
    bool saysOptimal = (isOpt != nullptr && isOpt->type == Json::BOOL &&
                        isOpt->boolean) || claimText == "optimal";
    rep.widthLabeledNonOptimal = !saysOptimal;
    if (saysOptimal) {
        // Only exact results may call themselves optimal.
        const Json* mode = resp.find("mode");
        if (mode == nullptr || mode->text != "exact") {
            err("non-exact response labels width as optimal");
            rep.widthLabeledNonOptimal = false;
        }
    }

    rep.valid = rep.errors.empty();
    return rep;
}

} // namespace tdw
