#include "index.hpp"

#include <algorithm>

namespace {

// sign( x_int(edge e at y=p.y) - p.x ), edge stored upward (a.y < b.y).
//   x_int - px = a.x + dx*(py-a.y)/dy - px
// sign == sign( dx*(py-a.y) - (px-a.x)*dy ).
int edgeCompareAtY(const IndexEdge& e, const Pt& p) {
    Int z = (e.b.x - e.a.x) * (p.y - e.a.y) -
            (p.x - e.a.x) * (e.b.y - e.a.y);
    if (z > 0) return 1;
    if (z < 0) return -1;
    return 0;
}

// Order of upward edges e1, e2 by x-intersection at the slab midline
// y = (yL + yR)/2. Ordering is constant inside one slab for a simple ring.
//   x(e) = a.x + dx*(y - a.y)/dy ; compared at y=(yL+yR)/2.
// Multiply by 2*dy1*dy2 (>0):
//   (2*a1.x*dy1 + dx1*(yL+yR-2*a1.y))*dy2
//  -(2*a2.x*dy2 + dx2*(yL+yR-2*a2.y))*dy1.
int midCompare(const RingIndex& idx, int32_t id1, int32_t id2,
               const Int& yL, const Int& yR) {
    const IndexEdge& e1 = idx.pool[id1];
    const IndexEdge& e2 = idx.pool[id2];
    Int y2 = yL + yR;
    Int dx1 = e1.b.x - e1.a.x, dy1 = e1.b.y - e1.a.y;
    Int dx2 = e2.b.x - e2.a.x, dy2 = e2.b.y - e2.a.y;
    Int lhs = (2 * e1.a.x * dy1 + dx1 * (y2 - 2 * e1.a.y)) * dy2;
    Int rhs = (2 * e2.a.x * dy2 + dx2 * (y2 - 2 * e2.a.y)) * dy1;
    Int z = lhs - rhs;
    if (z > 0) return 1;
    if (z < 0) return -1;
    // Deterministic tie-break (cannot cross inside a slab for a simple ring).
    auto ptCmp = [](const Pt& a, const Pt& b) {
        if (a.y != b.y) return a.y < b.y ? -1 : 1;
        if (a.x != b.x) return a.x < b.x ? -1 : 1;
        return 0;
    };
    int c = ptCmp(e1.a, e2.a);
    if (c) return c;
    return ptCmp(e1.b, e2.b);
}

bool onIndexEdge(const IndexEdge& e, const Pt& p) {
    return onSegment(p, e.a, e.b);
}

} // namespace

void RingIndex::build(const Ring& r) {
    ring = &r;
    lo = hi = r.v.front();
    for (const Pt& p : r.v) {
        lo.x = std::min(lo.x, p.x);
        lo.y = std::min(lo.y, p.y);
        hi.x = std::max(hi.x, p.x);
        hi.y = std::max(hi.y, p.y);
    }

    // Distinct y levels.
    levels.clear();
    for (size_t i = 0; i + 1 < r.v.size(); ++i) levels.push_back(r.v[i].y);
    std::sort(levels.begin(), levels.end());
    levels.erase(std::unique(levels.begin(), levels.end()), levels.end());
    const size_t m = levels.size();

    pool.clear();
    startAt.assign(m, {});
    endAt.assign(m, {});
    slab.assign(m > 0 ? m - 1 : 0, {});
    levelHoriz.assign(m, {});
    levelVerts.assign(m, {});

    auto levelIndex = [&](const Int& y) -> size_t {
        return static_cast<size_t>(
            std::lower_bound(levels.begin(), levels.end(), y) - levels.begin());
    };

    for (size_t i = 0; i + 1 < r.v.size(); ++i)
        levelVerts[levelIndex(r.v[i].y)].push_back(i);

    // Allocate one pool ID per non-horizontal edge.
    std::vector<int32_t> edgeId(r.v.size() - 1, -1);
    for (size_t i = 0; i + 1 < r.v.size(); ++i) {
        const Pt& a = r.v[i];
        const Pt& b = r.v[i + 1];
        if (a.y == b.y) {
            IndexEdge e;
            e.a = (a.x <= b.x) ? a : b;
            e.b = (a.x <= b.x) ? b : a;
            levelHoriz[levelIndex(a.y)].push_back(e);
            continue;
        }
        IndexEdge e;
        e.a = (a.y < b.y) ? a : b; // upward
        e.b = (a.y < b.y) ? b : a;
        int32_t id = static_cast<int32_t>(pool.size());
        pool.push_back(e);
        edgeId[i] = id;
        startAt[levelIndex(e.a.y)].push_back(id);
        endAt[levelIndex(e.b.y)].push_back(id);
    }

    // Sweep bottom -> top, maintaining active edges sorted by x at the
    // midline of the slab being entered.
    std::vector<int32_t> active;
    for (size_t k = 0; k + 1 < m; ++k) {
        // Edges ending at levels[k] leave the active set (they spanned the
        // previous slab but not the new one).
        if (!endAt[k].empty()) {
            std::vector<int32_t> leaving = endAt[k];
            std::sort(leaving.begin(), leaving.end());
            active.erase(
                std::remove_if(active.begin(), active.end(),
                               [&](int32_t id) {
                                   return std::binary_search(leaving.begin(),
                                                             leaving.end(), id);
                               }),
                active.end());
        }

        // Edges starting at levels[k] enter; insert by exact midline order.
        const Int& yL = levels[k];
        const Int& yR = levels[k + 1];
        for (int32_t id : startAt[k]) {
            auto it = std::lower_bound(
                active.begin(), active.end(), id,
                [&](int32_t existing, int32_t /*newId*/) {
                    return midCompare(*this, existing, id, yL, yR) < 0;
                });
            active.insert(it, id);
        }
        slab[k] = active;
    }
}

void PolygonIndex::build(const Polygon& p) {
    polygon = &p;
    outer.build(p.outer);
    hole.clear();
    hole.reserve(p.holes.size());
    for (const Ring& h : p.holes) {
        RingIndex ri;
        ri.build(h);
        hole.push_back(std::move(ri));
    }
}

namespace point_index {

Rel locateRing(const RingIndex& idx, const Pt& p) {
    if (p.x < idx.lo.x || p.x > idx.hi.x || p.y < idx.lo.y ||
        p.y > idx.hi.y)
        return Rel::Outside;

    const auto& levels = idx.levels;
    size_t k = static_cast<size_t>(
        std::lower_bound(levels.begin(), levels.end(), p.y) - levels.begin());

    if (k < levels.size() && levels[k] == p.y) {
        // Boundary via every incident vertex at this level (covers both
        // incident edges, including those ending here) and horizontal edges.
        const Ring& r = *idx.ring;
        for (size_t vi : idx.levelVerts[k]) {
            size_t prev = (vi == 0) ? r.v.size() - 2 : vi - 1;
            if (onSegment(p, r.v[vi], r.v[vi + 1]) ||
                onSegment(p, r.v[prev], r.v[vi]))
                return Rel::Bound;
        }
        for (const IndexEdge& e : idx.levelHoriz[k])
            if (onIndexEdge(e, p)) return Rel::Bound;

        // Half-open crossing candidates: edges with a.y == py
        // (startAt[k]) plus edges spanning across py (in slab[k-1] with
        // b.y > py). Edges ending at py are excluded. A zero comparison means
        // the point lies on that edge -> boundary (checked immediately).
        long crossings = 0;
        auto onActiveEdge = [&](int32_t id) {
            int s = edgeCompareAtY(idx.pool[id], p);
            if (s == 0) return true;
            if (s > 0) ++crossings; // intersection strictly to the right
            return false;
        };
        for (int32_t id : idx.startAt[k])
            if (onActiveEdge(id)) return Rel::Bound;
        if (k > 0)
            for (int32_t id : idx.slab[k - 1])
                if (idx.pool[id].b.y > p.y)
                    if (onActiveEdge(id)) return Rel::Bound;
        return (crossings & 1L) ? Rel::Inside : Rel::Outside;
    }

    // p.y strictly between levels[k-1] and levels[k].
    if (k == 0 || k >= levels.size()) return Rel::Outside;

    // Sorted edges for slab k-1; binary search by x intersection at p.y.
    const auto& edges = idx.slab[k - 1];
    auto cmp = [&](int32_t id, int /*tag*/) {
        // first position where x_int >= p.x  <=>  edgeCompareAtY >= 0
        return edgeCompareAtY(idx.pool[id], p) < 0;
    };
    size_t lo = static_cast<size_t>(
        std::lower_bound(edges.begin(), edges.end(), 0, cmp) - edges.begin());
    if (lo < edges.size() && edgeCompareAtY(idx.pool[edges[lo]], p) == 0)
        return Rel::Bound;
    long crossings = static_cast<long>(edges.size() - lo);
    return (crossings & 1L) ? Rel::Inside : Rel::Outside;
}

Rel locatePolygon(const PolygonIndex& idx, const Pt& p) {
    Rel r = locateRing(idx.outer, p);
    if (r == Rel::Outside) return Rel::Outside;
    if (r == Rel::Bound) return Rel::Bound;
    for (const RingIndex& h : idx.hole) {
        Rel hr = locateRing(h, p);
        if (hr == Rel::Bound) return Rel::Bound;
        if (hr == Rel::Inside) return Rel::Outside;
    }
    return Rel::Inside;
}

} // namespace point_index
