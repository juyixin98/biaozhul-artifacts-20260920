#pragma once
// Rollback-capable disjoint set union (union by size, NO path compression).
//
// Path compression is forbidden here on purpose: it mutates parent pointers
// outside the history log, which would make rollback() unable to restore the
// exact prior state. find() is therefore O(log n) (union by size keeps trees
// shallow) instead of near-constant.
#include <cstddef>
#include <utility>
#include <vector>

class RollbackDSU {
public:
    explicit RollbackDSU(int n) : parent_(n), size_(n, 1) {
        for (int i = 0; i < n; ++i) parent_[i] = i;
    }

    int find(int x) const {
        while (parent_[x] != x) x = parent_[x];
        return x;
    }

    bool connected(int a, int b) const { return find(a) == find(b); }

    // Returns false when a and b were already in the same set (a no-op record
    // is pushed so that checkpoint/rollback stays aligned).
    bool unite(int a, int b) {
        a = find(a);
        b = find(b);
        if (a == b) {
            history_.push_back({-1, -1, -1});
            return false;
        }
        if (size_[a] < size_[b]) std::swap(a, b);
        history_.push_back({b, a, size_[a]});
        parent_[b] = a;
        size_[a] += size_[b];
        return true;
    }

    std::size_t checkpoint() const { return history_.size(); }

    void rollback(std::size_t cp) {
        while (history_.size() > cp) {
            Record rec = history_.back();
            history_.pop_back();
            if (rec.child >= 0) {
                parent_[rec.child] = rec.child;
                size_[rec.parent] = rec.size;
            }
        }
    }

private:
    struct Record {
        int child;   // root that was attached (-1 for no-op)
        int parent;  // root it was attached to
        int size;    // size of parent before the merge
    };
    std::vector<int> parent_;
    std::vector<int> size_;
    std::vector<Record> history_;
};
