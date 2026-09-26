#include "dominance.hpp"

#include <algorithm>
#include <functional>
#include <stdexcept>
#include <cstdint>

namespace dom {

namespace {

// Dense bit set over node indices, used by the naive reference solver.
class BitSet {
public:
    explicit BitSet(int n) : words_((n + 63) / 64, 0) {}
    void set(int v) { words_[(size_t)v >> 6] |= 1ULL << (v & 63); }
    void clear(int v) { words_[(size_t)v >> 6] &= ~(1ULL << (v & 63)); }
    bool test(int v) const { return (words_[(size_t)v >> 6] >> (v & 63)) & 1ULL; }
    bool containsAll(const BitSet& o) const {
        for (size_t i = 0; i < words_.size(); ++i)
            if ((words_[i] & o.words_[i]) != o.words_[i]) return false;
        return true;
    }
    void intersectWith(const BitSet& o) {
        for (size_t i = 0; i < words_.size(); ++i) words_[i] &= o.words_[i];
    }
    bool operator==(const BitSet& o) const { return words_ == o.words_; }
    int popcount() const {
        int c = 0;
        for (uint64_t w : words_) c += __builtin_popcountll(w);
        return c;
    }
    std::vector<int> toSortedList() const {
        std::vector<int> out;
        for (size_t i = 0; i < words_.size(); ++i) {
            uint64_t w = words_[i];
            while (w) { int b = __builtin_ctzll(w); out.push_back((int)(i * 64 + b)); w &= w - 1; }
        }
        return out;
    }

private:
    std::vector<uint64_t> words_;
};

// Iterative DFS from entry; fills DFS numbering and parents.
// Returns preorder (indices 1..N accessible via vertex).
struct DFSNumbering {
    int nReachable = 0;
    std::vector<int> semi;     // DFS number, 0 = unreachable
    std::vector<int> vertex;   // dfs number -> node, 1-based; vertex[0] unused
    std::vector<int> parent;   // DFS-tree parent node
};

DFSNumbering runDFS(const Graph& g) {
    int n = g.n();
    DFSNumbering d;
    d.semi.assign(n, 0);
    d.parent.assign(n, -1);
    d.vertex.assign(1, -1);

    int counter = 0;
    d.semi[g.entry] = ++counter;
    d.vertex.push_back(g.entry);

    std::vector<std::pair<int, size_t>> st;
    st.emplace_back(g.entry, 0);
    while (!st.empty()) {
        int v = st.back().first;
        size_t& k = st.back().second;
        if (k < g.succ[v].size()) {
            int w = g.succ[v][k++];
            if (d.semi[w] == 0) {
                d.parent[w] = v;
                d.semi[w] = ++counter;
                d.vertex.push_back(w);
                st.emplace_back(w, 0);
            }
        } else {
            st.pop_back();
        }
    }
    d.nReachable = counter;
    return d;
}

} // namespace

LTResult lengauerTarjan(const Graph& g) {
    const int n = g.n();
    const DFSNumbering dfs = runDFS(g);
    const int N = dfs.nReachable;

    std::vector<int> semi = dfs.semi;
    const std::vector<int>& vertex = dfs.vertex;

    std::vector<int> ancestor(n, -1);   // node-based; -1 = not linked yet
    std::vector<int> label(n, -1);
    std::vector<int> idom(n, -1);
    std::vector<std::vector<int>> bucket(n);
    for (int v = 0; v < n; ++v) label[v] = v;

    // EVAL with path compression (Lengauer-Tarjan 1979, Figure 4 style).
    auto compress = [&](auto&& self, int v) -> void {
        if (ancestor[ancestor[v]] != -1) {
            self(self, ancestor[v]);
            if (semi[label[ancestor[v]]] < semi[label[v]])
                label[v] = label[ancestor[v]];
            ancestor[v] = ancestor[ancestor[v]];
        }
    };
    auto eval = [&](int v) -> int {
        if (ancestor[v] == -1) return v;
        compress(compress, v);
        return label[v];
    };

    for (int i = N; i >= 2; --i) {
        int w = vertex[i];
        for (int v : g.pred[w]) {
            if (semi[v] == 0) continue; // edge from unreachable node: ignore
            int u = eval(v);
            if (semi[u] < semi[w]) semi[w] = semi[u];
        }
        bucket[vertex[semi[w]]].push_back(w);

        // LINK(parent[w], w): simple forest link (the classic O(E log V) variant
        // of LT; still no external solver, and well within the bounded scale).
        ancestor[w] = dfs.parent[w];

        int pw = dfs.parent[w];
        for (int v : bucket[pw]) {
            int u = eval(v);
            idom[v] = (semi[u] < semi[v]) ? u : pw;
        }
        bucket[pw].clear();
    }
    for (int i = 2; i <= N; ++i) {
        int w = vertex[i];
        if (idom[w] != vertex[semi[w]]) idom[w] = idom[idom[w]];
    }
    idom[vertex[1]] = vertex[1];

    // Dominance frontiers via the Cooper-Harvey-Kennedy idom-tree walk.
    std::vector<std::vector<int>> df(n);
    for (int bi = 1; bi <= N; ++bi) {
        int b = vertex[bi];
        int idomB = idom[b];
        for (int p : g.pred[b]) {
            if (semi[p] == 0) continue;
            int runner = p;
            while (runner != idomB) {
                df[runner].push_back(b);
                runner = idom[runner];
            }
        }
    }
    // The CHK walk misses exactly one formal-definition case (Cytron et al.):
    // the entry node lying in its own dominance frontier. The runner for block
    // entry stops at idom(entry)=entry without recording, but formally entry
    // is in DF(entry) whenever entry has a reachable predecessor p that entry
    // dominates (it dominates every reachable node) while entry is not a
    // STRICT dominator of itself. That covers both an entry self edge and a
    // back/cross edge into the entry.
    for (int p : g.pred[g.entry]) {
        if (semi[p] != 0) { df[g.entry].push_back(g.entry); break; }
    }
    for (int v = 0; v < n; ++v) {
        std::sort(df[v].begin(), df[v].end());
        df[v].erase(std::unique(df[v].begin(), df[v].end()), df[v].end());
    }

    LTResult r;
    r.reachable = [&] {
        std::vector<char> rc(n, 0);
        for (int i = 1; i <= N; ++i) rc[vertex[i]] = 1;
        return rc;
    }();
    r.preorder = std::vector<int>(vertex.begin() + 1, vertex.end());
    r.idom = std::move(idom);
    r.df = std::move(df);
    r.reachableCount = N;
    return r;
}

NaiveResult naiveIterative(const Graph& g) {
    const int n = g.n();
    std::vector<char> reach = g.reachableFromEntry();

    // Reverse postorder over the reachable subgraph.
    std::vector<int> postorder;
    postorder.reserve(n);
    std::vector<char> vis(n, 0);
    std::vector<std::pair<int, size_t>> st;
    st.emplace_back(g.entry, 0);
    vis[g.entry] = 1;
    while (!st.empty()) {
        int v = st.back().first;
        size_t& k = st.back().second;
        if (k < g.succ[v].size()) {
            int w = g.succ[v][k++];
            if (reach[w] && !vis[w]) { vis[w] = 1; st.emplace_back(w, 0); }
        } else {
            postorder.push_back(v);
            st.pop_back();
        }
    }
    std::vector<int> rpo(postorder.rbegin(), postorder.rend());

    BitSet all(n);
    for (int v = 0; v < n; ++v) if (reach[v]) all.set(v);

    std::vector<BitSet> dom(n, BitSet(n));
    for (int v = 0; v < n; ++v) {
        if (reach[v]) dom[v] = all;
    }
    // Dom(entry) = {entry}
    BitSet entrySet(n);
    entrySet.set(g.entry);
    dom[g.entry] = entrySet;

    int iterations = 0;
    bool changed = true;
    while (changed) {
        changed = false;
        ++iterations;
        for (int b : rpo) {
            if (b == g.entry) continue;
            BitSet acc(n);
            bool have = false;
            for (int p : g.pred[b]) {
                if (!reach[p]) continue;
                if (!have) { acc = dom[p]; have = true; }
                else acc.intersectWith(dom[p]);
            }
            acc.set(b);
            if (!(acc == dom[b])) { dom[b] = acc; changed = true; }
        }
    }

    NaiveResult r;
    r.iterations = iterations;
    r.idom.assign(n, -1);
    r.domSets.resize(n);
    r.df.resize(n);
    for (int v = 0; v < n; ++v) {
        if (!reach[v]) continue;
        r.domSets[v] = dom[v].toSortedList();
    }
    r.idom[g.entry] = g.entry;
    for (int b : rpo) {
        if (b == g.entry) continue;
        int best = -1, bestSize = -1;
        for (int d : r.domSets[b]) {
            if (d == b) continue;
            int sz = dom[d].popcount();
            if (sz > bestSize) { bestSize = sz; best = d; }
        }
        r.idom[b] = best;
    }

    // Frontier directly from the set definition:
    // n in DF(b) iff exists reachable pred p of n with b in Dom(p)
    //                        and not (b in Dom(n) and b != n).
    std::vector<std::vector<char>> inDom(n);
    for (int v = 0; v < n; ++v) {
        inDom[v].assign(n, 0);
        if (reach[v]) for (int d : r.domSets[v]) inDom[v][d] = 1;
    }
    for (int nn = 0; nn < n; ++nn) {
        if (!reach[nn]) continue;
        for (int p : g.pred[nn]) {
            if (!reach[p]) continue;
            for (int b : r.domSets[p]) {
                bool strictlyDominatesN = (b != nn && inDom[nn][b]);
                if (!strictlyDominatesN) r.df[b].push_back(nn);
            }
        }
    }
    for (int v = 0; v < n; ++v) {
        std::sort(r.df[v].begin(), r.df[v].end());
        r.df[v].erase(std::unique(r.df[v].begin(), r.df[v].end()), r.df[v].end());
    }
    return r;
}

PathEnumResult pathEnumeration(const Graph& g, long long capPaths) {
    const int n = g.n();
    std::vector<char> reach = g.reachableFromEntry();

    BitSet all(n);
    for (int v = 0; v < n; ++v) if (reach[v]) all.set(v);
    std::vector<BitSet> dom(n, BitSet(n));
    for (int v = 0; v < n; ++v) if (reach[v]) dom[v] = all;

    std::vector<char> onPath(n, 0);
    long long total = 0;
    bool capped = false;

    // Enumerate every simple path starting at the entry. Arrival at node v
    // along a simple path constitutes one entry->v path, whose node set is
    // intersected into dom[v].
    std::function<void(int, BitSet&)> dfs = [&](int v, BitSet& path) {
        if (capped) return;
        ++total;
        if (total > capPaths) { capped = true; return; }
        dom[v].intersectWith(path);
        for (int w : g.succ[v]) {
            if (!reach[w] || path.test(w)) continue;
            path.set(w);
            dfs(w, path);
            path.clear(w);
        }
    };

    BitSet initial(n);
    initial.set(g.entry);
    dfs(g.entry, initial);
    if (capped)
        throw std::runtime_error("path enumeration cap exceeded (" + std::to_string(capPaths) + ")");

    PathEnumResult r;
    r.totalPaths = total;
    r.domSets.resize(n);
    for (int v = 0; v < n; ++v) {
        if (reach[v]) r.domSets[v] = dom[v].toSortedList();
    }
    return r;
}

bool dominates(const LTResult& r, int a, int b) {
    if (a < 0 || b < 0 || a >= (int)r.reachable.size() || b >= (int)r.reachable.size()) return false;
    if (a == b) return true; // reflexive: every node "dominates itself"
    if (!r.reachable[a] || !r.reachable[b]) return false;
    int x = b;
    while (x != r.idom[x]) {
        x = r.idom[x];
        if (x == a) return true;
    }
    return false;
}

bool properlyDominates(const LTResult& r, int a, int b) {
    return a != b && dominates(r, a, b);
}

bool sameIdom(const std::vector<int>& x, const std::vector<int>& y) { return x == y; }

bool sameDomSets(const std::vector<std::vector<int>>& x,
                 const std::vector<std::vector<int>>& y) {
    if (x.size() != y.size()) return false;
    for (size_t i = 0; i < x.size(); ++i) if (x[i] != y[i]) return false;
    return true;
}

bool sameFrontiers(const std::vector<std::vector<int>>& x,
                   const std::vector<std::vector<int>>& y) {
    return sameDomSets(x, y);
}

} // namespace dom
