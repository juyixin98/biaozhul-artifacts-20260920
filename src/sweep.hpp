#pragma once
//
// sweep.hpp — axis-aligned rectangle union, offline sweep-line engine.
//
// Semantics (see README.md):
//   * Every rectangle is the HALF-OPEN region  [x1,x2) x [y1,y2), integer grid.
//   * A rectangle with x1 == x2 or y1 == y2 is an empty region (line/point on
//     the grid); it contributes neither area nor boundary.
//   * Shared edges (adjacent rectangles, overlaps, nesting) are counted zero
//     extra times: boundary length equals the measure of the symmetric
//     difference of the union cross-section before/after each event group.
//
// Complexity: O(n log n) time, O(n) memory.
// Arithmetic: coordinates are int64 (bounds validated by the caller), all
// accumulation uses signed 128 bit to make overflow detectable upstream.
//
#include <cstdint>
#include <vector>
#include <algorithm>

namespace ru {

using i64 = long long;
using i128 = __int128_t;

struct Rect {
    i64 x1 = 0, y1 = 0, x2 = 0, y2 = 0;
};

struct Metrics {
    i128 area = 0;
    i128 perimeter = 0;
};

namespace detail {

struct Event {
    i64 p;          // sweep coordinate
    int kind;       // +1 = left/lower edge (add), -1 = right/upper edge (remove)
    i64 lo, hi;     // covered interval on the other axis, [lo, hi)
};

// Classic interval-cover segment tree over compressed coordinates.
// Node covers segment-index range [l, r); len is the covered length.
class CoverTree {
public:
    explicit CoverTree(const std::vector<i64>& coords)
        : z_(coords), nodes_(4 * (coords.size() - 1)) {}

    void add(int ql, int qr, int delta) {
        update(1, 0, static_cast<int>(z_.size() - 1), ql, qr, delta);
    }

    i64 coveredLength() const { return nodes_[1].len; }

private:
    struct Node {
        int cover = 0;
        i64 len = 0;
    };


    void pull(int node, int l, int r) {
        if (nodes_[static_cast<size_t>(node)].cover > 0) {
            nodes_[static_cast<size_t>(node)].len = z_[static_cast<size_t>(r)] -
                                                   z_[static_cast<size_t>(l)];
        } else if (l + 1 == r) {
            nodes_[static_cast<size_t>(node)].len = 0;
        } else {
            nodes_[static_cast<size_t>(node)].len =
                nodes_[static_cast<size_t>(node * 2)].len +
                nodes_[static_cast<size_t>(node * 2 + 1)].len;
        }
    }

    void update(int node, int l, int r, int ql, int qr, int delta) {
        if (qr <= l || r <= ql) return;
        if (ql <= l && r <= qr) {
            nodes_[static_cast<size_t>(node)].cover += delta;
        } else {
            int m = l + (r - l) / 2;
            update(node * 2, l, m, ql, qr, delta);
            update(node * 2 + 1, m, r, ql, qr, delta);
        }
        pull(node, l, r);
    }

    const std::vector<i64>& z_;
    std::vector<Node> nodes_;
};

// One sweep. If collectArea, integrates covered length between event slabs.
// Always returns the total exposed transverse boundary length:
// at each event group g at coordinate p, with A the cross-section just before
// and B just after, the contribution is |B \\ A| + |A \\ B| (symmetric
// difference). It is accumulated without an O(n) per-event walk:
//   * apply every add one at a time, summing the covered-length increase
//     (telescopes to |(A U adds) \\ A| = |B \\ A|, because at this stage the
//      still-present ending rectangles are part of the coverage);
//   * then every remove one at a time, summing the decrease
//     (telescopes to |(A U adds) \\ B| = |A \\ B|).
// Adds must be processed before removes within a group; event sorting puts
// kind == +1 first.
inline i128 sweepOnce(const std::vector<Rect>& rects, bool horizontal, i128& boundaryOut) {
    std::vector<Event> events;
    std::vector<i64> coords;
    events.reserve(rects.size() * 2);
    coords.reserve(rects.size() * 2);

    for (const Rect& r : rects) {
        if (r.x1 == r.x2 || r.y1 == r.y2) continue;  // empty half-open box
        if (!horizontal) {
            events.push_back({r.x1, +1, r.y1, r.y2});
            events.push_back({r.x2, -1, r.y1, r.y2});
            coords.push_back(r.y1);
            coords.push_back(r.y2);
        } else {
            events.push_back({r.y1, +1, r.x1, r.x2});
            events.push_back({r.y2, -1, r.x1, r.x2});
            coords.push_back(r.x1);
            coords.push_back(r.x2);
        }
    }

    boundaryOut = 0;
    if (events.empty()) return 0;

    std::sort(events.begin(), events.end(),
              [](const Event& a, const Event& b) {
                  if (a.p != b.p) return a.p < b.p;
                  return a.kind > b.kind;  // adds before removes at same p
              });
    std::sort(coords.begin(), coords.end());
    coords.erase(std::unique(coords.begin(), coords.end()), coords.end());

    auto indexOf = [&](i64 v) {
        return static_cast<int>(
            std::lower_bound(coords.begin(), coords.end(), v) - coords.begin());
    };

    CoverTree tree(coords);
    i128 area = 0;

    for (size_t i = 0; i < events.size();) {
        size_t j = i;
        const i64 p = events[i].p;

        // Phase 1: starting edges.
        while (j < events.size() && events[j].p == p && events[j].kind == +1) {
            i64 before = tree.coveredLength();
            tree.add(indexOf(events[j].lo), indexOf(events[j].hi), +1);
            i64 after = tree.coveredLength();
            boundaryOut += static_cast<i128>(after - before);
            ++j;
        }
        // Phase 2: ending edges.
        while (j < events.size() && events[j].p == p && events[j].kind == -1) {
            i64 before = tree.coveredLength();
            tree.add(indexOf(events[j].lo), indexOf(events[j].hi), -1);
            i64 after = tree.coveredLength();
            boundaryOut += static_cast<i128>(before - after);
            ++j;
        }

        // Active set now holds throughout the slab [p, next_p).
        if (j < events.size()) {
            area += static_cast<i128>(tree.coveredLength()) *
                    static_cast<i128>(events[j].p - p);
        }
        i = j;
    }

    return area;
}

}  // namespace detail

// Union area and perimeter of the given (validated, normalized) rectangles.
// Degenerate boxes are skipped internally.
inline Metrics computeUnionMetrics(const std::vector<Rect>& rects) {
    Metrics m;
    i128 verticalEdges = 0;
    i128 horizontalEdges = 0;
    m.area = detail::sweepOnce(rects, /*horizontal=*/false, verticalEdges);
    (void)detail::sweepOnce(rects, /*horizontal=*/true, horizontalEdges);
    m.perimeter = verticalEdges + horizontalEdges;
    return m;
}

}  // namespace ru
