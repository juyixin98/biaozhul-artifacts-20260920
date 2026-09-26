// Directed graph (control-flow graph model) with bounded-size validation.
#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <queue>
#include <stdexcept>
#include <cstdint>
#include <algorithm>

namespace dom {

struct Limits {
    // Deliberate scale bounds for this bounded, verifiable backend.
    int maxNodes = 5000;
    int maxEdges = 50000;
    int maxLabelLen = 128;
};

// A simple directed graph. Nodes carry string labels; edges are label pairs.
// Internally nodes are compacted to indices [0, n).
class Graph {
public:
    Limits limits;
    std::vector<std::string> labels;
    std::vector<std::vector<int>> succ; // adjacency lists
    std::vector<std::vector<int>> pred; // reverse adjacency lists
    int entry = -1;

    // Builds a graph. Duplicate edges are kept once; self loops allowed.
    // Throws std::runtime_error on any validation failure.
    static Graph build(const std::vector<std::string>& nodeLabels,
                       const std::vector<std::pair<std::string, std::string>>& edges,
                       const std::string& entryLabel,
                       const Limits& lim = Limits{}) {
        Graph g;
        g.limits = lim;
        if (nodeLabels.empty()) throw std::runtime_error("graph must contain at least one node");
        if ((int)nodeLabels.size() > lim.maxNodes)
            throw std::runtime_error("too many nodes (limit " + std::to_string(lim.maxNodes) + ")");
        if ((int)edges.size() > lim.maxEdges)
            throw std::runtime_error("too many edges (limit " + std::to_string(lim.maxEdges) + ")");

        std::unordered_map<std::string, int> id;
        id.reserve(nodeLabels.size() * 2);
        for (const auto& lbl : nodeLabels) {
            if (lbl.empty()) throw std::runtime_error("node label must not be empty");
            if ((int)lbl.size() > lim.maxLabelLen) throw std::runtime_error("node label too long");
            if (!id.emplace(lbl, (int)g.labels.size()).second)
                throw std::runtime_error("duplicate node label: " + lbl);
            g.labels.push_back(lbl);
        }
        auto it = id.find(entryLabel);
        if (it == id.end()) throw std::runtime_error("entry node not found: " + entryLabel);
        g.entry = it->second;

        int n = (int)g.labels.size();
        g.succ.assign(n, {});
        g.pred.assign(n, {});
        std::vector<std::unordered_set<int>> seen(n);
        for (const auto& e : edges) {
            auto a = id.find(e.first);
            auto b = id.find(e.second);
            if (a == id.end()) throw std::runtime_error("edge references unknown node: " + e.first);
            if (b == id.end()) throw std::runtime_error("edge references unknown node: " + e.second);
            int u = a->second, v = b->second;
            if (seen[u].insert(v).second) {
                g.succ[u].push_back(v);
                g.pred[v].push_back(u);
            }
        }
        for (int i = 0; i < n; ++i) {
            std::sort(g.succ[i].begin(), g.succ[i].end());
            std::sort(g.pred[i].begin(), g.pred[i].end());
        }
        return g;
    }

    int n() const { return (int)labels.size(); }

    // Nodes reachable from the entry node (includes entry itself).
    std::vector<char> reachableFromEntry() const {
        int n = this->n();
        std::vector<char> vis(n, 0);
        std::queue<int> q;
        vis[entry] = 1;
        q.push(entry);
        while (!q.empty()) {
            int u = q.front(); q.pop();
            for (int v : succ[u]) if (!vis[v]) { vis[v] = 1; q.push(v); }
        }
        return vis;
    }
};

} // namespace dom
