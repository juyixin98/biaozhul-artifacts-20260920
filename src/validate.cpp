#include "validate.hpp"

#include <algorithm>
#include <queue>
#include <set>

namespace {

std::set<int> toSet(const std::vector<int>& xs) {
    return std::set<int>(xs.begin(), xs.end());
}

}  // namespace

VerificationResult verifyDecomposition(const Graph& g,
                                       const TreeDecomposition& td) {
    VerificationResult res;
    res.bagCount = td.bags.size();
    const int n = g.n();

    if (td.bags.empty()) {
        if (n == 0) {
            res.ok = true;
            res.width = -1;
        } else {
            res.errors.push_back("decomposition has no bags but graph is nonempty");
        }
        return res;
    }

    // T1: bag contents valid, no duplicates, nonempty.
    for (const TDBag& bag : td.bags) {
        if (bag.id < 0 || bag.id >= static_cast<int>(td.bags.size()))
            res.errors.push_back("bag has invalid id " +
                                 std::to_string(bag.id));
        std::set<int> seen;
        if (bag.vertices.empty())
            res.errors.push_back("bag " + std::to_string(bag.id) + " is empty");
        for (int v : bag.vertices) {
            if (v < 0 || v >= n)
                res.errors.push_back("bag " + std::to_string(bag.id) +
                                     " contains invalid vertex " +
                                     std::to_string(v));
            if (!seen.insert(v).second)
                res.errors.push_back("bag " + std::to_string(bag.id) +
                                     " has duplicate vertex " + std::to_string(v));
        }
        res.width = std::max(res.width,
                             static_cast<int>(bag.vertices.size()) - 1);
    }

    // T2: every graph edge covered by at least one bag.
    auto edges = g.edgeList();
    res.edgesTotal = static_cast<long>(edges.size());
    std::set<std::pair<int, int>> coveredEdges;
    for (const TDBag& bag : td.bags) {
        std::set<int> members = toSet(bag.vertices);
        for (int a : members)
            for (int b : members)
                if (a < b) coveredEdges.insert({a, b});
    }
    for (auto [u, v] : edges) {
        if (coveredEdges.count({u, v})) {
            ++res.edgesCovered;
        } else {
            res.errors.push_back("edge {" + g.labels[u] + ", " +
                                 g.labels[v] + "} is in no bag");
        }
    }

    // Vertex coverage (every vertex, incl. isolated ones, appears somewhere).
    // Invalid ids were already reported in T1; skip them here to stay in bounds.
    std::vector<char> seenVertex(n, 0);
    for (const TDBag& bag : td.bags)
        for (int v : bag.vertices)
            if (v >= 0 && v < n) seenVertex[v] = 1;
    for (int v = 0; v < n; ++v)
        if (!seenVertex[v])
            res.errors.push_back("vertex " + g.labels[v] +
                                 " appears in no bag");

    // T4: the bag graph is a single undirected tree.
    int B = static_cast<int>(td.bags.size());
    std::vector<std::set<int>> treeAdj(B);
    std::set<std::pair<int, int>> uniqueEdges;
    auto addTreeEdge = [&](int a, int b) {
        if (a < 0 || a >= B || b < 0 || b >= B) {
            res.errors.push_back("tree edge references invalid bag id");
            return;
        }
        if (a == b) {
            res.errors.push_back("self-loop in decomposition tree at bag " +
                                 std::to_string(a));
            return;
        }
        int lo = std::min(a, b), hi = std::max(a, b);
        if (!uniqueEdges.insert({lo, hi}).second)
            res.errors.push_back("duplicate tree edge between bags " +
                                 std::to_string(lo) + " and " +
                                 std::to_string(hi));
        treeAdj[lo].insert(hi);
        treeAdj[hi].insert(lo);
    };
    for (auto [a, b] : td.treeEdges) addTreeEdge(a, b);
    for (auto [a, b] : td.rootJoinEdges) addTreeEdge(a, b);

    std::vector<char> vis(B, 0);
    std::queue<int> q;
    q.push(0);
    vis[0] = 1;
    int reached = 0;
    while (!q.empty()) {
        int x = q.front();
        q.pop();
        ++reached;
        for (int y : treeAdj[x])
            if (!vis[y]) { vis[y] = 1; q.push(y); }
    }
    res.treeConnected = reached == B;
    if (!res.treeConnected)
        res.errors.push_back("decomposition tree is disconnected (reached " +
                             std::to_string(reached) + " of " +
                             std::to_string(B) + " bags)");
    if (static_cast<int>(uniqueEdges.size()) != B - 1)
        res.errors.push_back("decomposition has " +
                             std::to_string(uniqueEdges.size()) +
                             " tree edges but " + std::to_string(B) +
                             " bags (a tree needs exactly B-1)");

    // T3: running intersection — bags containing each vertex are connected.
    for (int v = 0; v < n; ++v) {
        std::vector<char> contains(B, 0);
        int count = 0;
        for (const TDBag& bag : td.bags) {
            if (bag.id < 0 || bag.id >= B) continue;  // reported in T1
            for (int x : bag.vertices)
                if (x == v) contains[bag.id] = 1;
        }
        for (char c : contains) count += c;
        if (count == 0) continue;  // reported already above
        int start = 0;
        while (!contains[start]) ++start;
        std::vector<char> seen2(B, 0);
        std::queue<int> q2;
        q2.push(start);
        seen2[start] = 1;
        int reached2 = 0;
        while (!q2.empty()) {
            int x = q2.front();
            q2.pop();
            if (!contains[x]) continue;
            ++reached2;
            for (int y : treeAdj[x])
                if (!seen2[y]) { seen2[y] = 1; q2.push(y); }
        }
        if (reached2 != count)
            res.errors.push_back(
                "running intersection violated for vertex " + g.labels[v] +
                " (bags containing it form " + std::to_string(count) +
                " nodes but only " + std::to_string(reached2) +
                " are connected via such bags)");
    }

    if (res.width != td.width)
        res.warnings.push_back(
            "reported width " + std::to_string(td.width) +
            " disagrees with recomputed max|bag|-1 = " +
            std::to_string(res.width));

    res.ok = res.errors.empty();
    return res;
}

bool verifyElimination(const Graph& g, const EliminationResult& r,
                       std::vector<std::string>* errors) {
    auto err = [&](const std::string& m) {
        if (errors) errors->push_back(m);
    };
    bool ok = true;
    const int n = g.n();

    if (static_cast<int>(r.order.size()) != n) {
        err("order length " + std::to_string(r.order.size()) +
            " != vertex count " + std::to_string(n));
        return false;
    }
    std::set<int> perm(r.order.begin(), r.order.end());
    if (static_cast<int>(perm.size()) != n ||
        (n > 0 && *perm.begin() != 0) ||
        (n > 0 && *perm.rbegin() != n - 1)) {
        err("order is not a permutation of 0..n-1");
        return false;  // cannot replay safely with out-of-range indices
    }

    // Independent replay.
    std::vector<std::vector<char>> adj(n, std::vector<char>(n, 0));
    for (int u = 0; u < n; ++u)
        for (int v = 0; v < n; ++v) adj[u][v] = g.adj[u][v];
    std::vector<char> alive(n, 1);
    long long fillSeen = 0;
    int width = n == 0 ? -1 : 0;

    if (static_cast<int>(r.steps.size()) != n) {
        err("step count " + std::to_string(r.steps.size()) +
            " != vertex count");
        ok = false;
    }

    for (int i = 0; i < n; ++i) {
        int v = r.order[i];
        std::vector<int> nb;
        for (int u = 0; u < n; ++u)
            if (u != v && alive[u] && adj[v][u]) nb.push_back(u);
        std::sort(nb.begin(), nb.end());
        width = std::max(width, static_cast<int>(nb.size()));

        if (i < static_cast<int>(r.steps.size())) {
            const ElimStep& s = r.steps[i];
            if (s.vertex != v) {
                err("step " + std::to_string(i) + " vertex mismatch");
                ok = false;
            }
            if (s.degreeAtElimination != static_cast<int>(nb.size())) {
                err("step " + std::to_string(i) + " reported degree " +
                    std::to_string(s.degreeAtElimination) + ", recomputed " +
                    std::to_string(nb.size()));
                ok = false;
            }
            std::set<int> expectedBag;
            expectedBag.insert(v);
            for (int u : nb) expectedBag.insert(u);
            if (toSet(s.bag) != expectedBag) {
                err("step " + std::to_string(i) + " bag mismatch");
                ok = false;
            }
            // Reported fill edges must be exactly the missing pairs.
            std::set<std::pair<int, int>> expectedFill;
            for (size_t a = 0; a < nb.size(); ++a)
                for (size_t b = a + 1; b < nb.size(); ++b)
                    if (!adj[nb[a]][nb[b]])
                        expectedFill.insert(
                            {std::min(nb[a], nb[b]), std::max(nb[a], nb[b])});
            std::set<std::pair<int, int>> gotFill;
            for (auto [x, y] : s.addedFill)
                gotFill.insert({std::min(x, y), std::max(x, y)});
            if (gotFill != expectedFill) {
                err("step " + std::to_string(i) + " fill edge set mismatch");
                ok = false;
            }
            fillSeen += static_cast<long long>(s.addedFill.size());
        }

        for (size_t a = 0; a < nb.size(); ++a)
            for (size_t b = a + 1; b < nb.size(); ++b)
                adj[nb[a]][nb[b]] = adj[nb[b]][nb[a]] = 1;
        alive[v] = 0;
    }

    if (r.width != width) {
        err("reported width " + std::to_string(r.width) +
            " != recomputed " + std::to_string(width));
        ok = false;
    }
    if (r.totalFill != fillSeen) {
        err("reported totalFill " + std::to_string(r.totalFill) +
            " != recomputed " + std::to_string(fillSeen));
        ok = false;
    }
    return ok;
}

bool verifyBagsMatchOrder(const Graph& g, const std::vector<int>& order,
                          const TreeDecomposition& td,
                          std::vector<std::string>* errors) {
    auto err = [&](const std::string& m) {
        if (errors) errors->push_back(m);
    };
    const int n = g.n();
    if (static_cast<int>(td.bags.size()) != n) {
        err("bag count " + std::to_string(td.bags.size()) + " != n " +
            std::to_string(n));
        return false;
    }

    // Independent replay recording the expected elimination bag per step.
    std::vector<std::vector<char>> adj(n, std::vector<char>(n, 0));
    for (int u = 0; u < n; ++u)
        for (int v = 0; v < n; ++v) adj[u][v] = g.adj[u][v];
    std::vector<char> alive(n, 1);
    bool ok = true;
    for (int i = 0; i < n; ++i) {
        int v = order[i];
        std::set<int> expected;
        expected.insert(v);
        for (int u = 0; u < n; ++u)
            if (u != v && alive[u] && adj[v][u]) expected.insert(u);
        if (td.bags[i].id != i) {
            err("bag " + std::to_string(i) + " has id " +
                std::to_string(td.bags[i].id));
            ok = false;
        }
        if (toSet(td.bags[i].vertices) != expected) {
            err("bag " + std::to_string(i) +
                " contents do not match elimination neighborhood");
            ok = false;
        }
        std::vector<int> nb(expected.begin(), expected.end());
        for (size_t a = 0; a < nb.size(); ++a)
            for (size_t b = a + 1; b < nb.size(); ++b)
                adj[nb[a]][nb[b]] = adj[nb[b]][nb[a]] = 1;
        alive[v] = 0;
    }
    return ok;
}
