#include "locator.hpp"

#include <algorithm>
#include <cmath>

namespace locator {

size_t Polygon::totalVertices() const {
    size_t n = outer.size();
    for (const auto& h : holes) n += h.size();
    return n;
}

namespace {

Location ringLocateEdges(const std::vector<Point>& ring, const Point& p) {
    return geo::pointInRing(p, ring);
}

} // namespace

bool buildPolygon(std::vector<Point> outer,
                  std::vector<std::vector<Point>> holes,
                  Polygon& out, BuildError& err) {
    auto describe = [](geo::RingError& e) {
        using K = geo::RingErrorKind;
        switch (e.kind) {
            case K::DuplicatePoint: return "duplicate consecutive point";
            case K::TooFewPoints: return "too few points";
            case K::DegenerateArea: return "degenerate (zero-area) ring";
            case K::SelfIntersection: return "self-intersecting ring";
        }
        return "invalid ring";
    };

    geo::RingError re;
    if (!geo::validateRing(outer, re)) {
        err.kind = BuildErrorKind::InvalidRing;
        err.which = "outer";
        err.ringError = re;
        err.detail = std::string("outer ring invalid: ") + describe(re) +
                     " — " + re.detail;
        return false;
    }
    for (size_t h = 0; h < holes.size(); ++h) {
        if (!geo::validateRing(holes[h], re)) {
            err.kind = BuildErrorKind::InvalidRing;
            err.which = "holes[" + std::to_string(h) + "]";
            err.ringError = re;
            err.detail = std::string("hole ring invalid: ") + describe(re) +
                         " — " + re.detail;
            return false;
        }
    }

    // Hole containment: rings are simple and the boundary check below rules
    // out touching, so a hole crossing the outer boundary would show both
    // strictly-inside and strictly-outside vertices. Require every hole
    // vertex to be strictly inside the outer ring.
    for (size_t h = 0; h < holes.size(); ++h) {
        int inside = 0;
        for (const Point& v : holes[h]) {
            Location loc = ringLocateEdges(outer, v);
            if (loc == Location::Boundary) {
                err.kind = BuildErrorKind::HoleOutsideOuter;
                err.which = "holes[" + std::to_string(h) + "]";
                err.detail = "hole touches outer ring boundary";
                return false;
            }
            if (loc == Location::Inside) ++inside;
        }
        if (inside == 0) {
            err.kind = BuildErrorKind::HoleOutsideOuter;
            err.which = "holes[" + std::to_string(h) + "]";
            err.detail = "hole is not inside the outer ring";
            return false;
        }
        if (inside != static_cast<int>(holes[h].size())) {
            err.kind = BuildErrorKind::HoleOutsideOuter;
            err.which = "holes[" + std::to_string(h) + "]";
            err.detail = "hole crosses the outer ring boundary "
                         "(some vertices inside, some outside)";
            return false;
        }
    }

    // Hole/hole relations: boundaries must not cross or touch, and holes
    // must not nest (one inside another). Disjoint only.
    for (size_t i = 0; i < holes.size(); ++i) {
        for (size_t j = i + 1; j < holes.size(); ++j) {
            bool touch = false;
            for (size_t e = 0; e < holes[i].size() && !touch; ++e) {
                const Point& a = holes[i][e];
                const Point& b = holes[i][(e + 1) % holes[i].size()];
                for (size_t f = 0; f < holes[j].size(); ++f) {
                    const Point& c = holes[j][f];
                    const Point& d = holes[j][(f + 1) % holes[j].size()];
                    if (geo::segmentsProperlyCross(a, b, c, d) ||
                        geo::pointOnSegment(a, c, d) ||
                        geo::pointOnSegment(b, c, d) ||
                        geo::pointOnSegment(c, a, b) ||
                        geo::pointOnSegment(d, a, b)) {
                        touch = true;
                        break;
                    }
                }
            }
            if (touch) {
                err.kind = BuildErrorKind::HolesIntersectOrNested;
                err.which = "holes[" + std::to_string(i) + "], holes[" +
                            std::to_string(j) + "]";
                err.detail = "hole boundaries cross or touch";
                return false;
            }
            // Nested? A vertex of j inside i (or vice versa).
            Location vji = ringLocateEdges(holes[i], holes[j][0]);
            Location vij = ringLocateEdges(holes[j], holes[i][0]);
            if (vji != Location::Outside || vij != Location::Outside) {
                err.kind = BuildErrorKind::HolesIntersectOrNested;
                err.which = "holes[" + std::to_string(i) + "], holes[" +
                            std::to_string(j) + "]";
                err.detail = "holes must be disjoint; nested holes are not allowed";
                return false;
            }
        }
    }

    out.outer = std::move(outer);
    out.holes = std::move(holes);
    return true;
}

Location locateNaive(const Polygon& poly, const Point& p) {
    Location outer = ringLocateEdges(poly.outer, p);
    if (outer != Location::Inside) return outer;
    for (const auto& hole : poly.holes) {
        Location hl = ringLocateEdges(hole, p);
        if (hl == Location::Boundary) return Location::Boundary;
        if (hl == Location::Inside) return Location::Outside;
    }
    return Location::Inside;
}

// --- horizontal slab index ------------------------------------------------

void GridIndex::build(const Polygon& poly) {
    rings_.clear();
    edgeCount_ = 0;
    auto addRing = [&](const std::vector<Point>& ring) {
        RingData rd;
        rd.edges.reserve(ring.size());
        for (size_t i = 0; i < ring.size(); ++i)
            rd.edges.push_back({ring[i], ring[(i + 1) % ring.size()]});
        rings_.push_back(std::move(rd));
        edgeCount_ += ring.size();
    };
    addRing(poly.outer);
    for (const auto& h : poly.holes) addRing(h);

    // Global bounding box.
    xmin_ = xmax_ = poly.outer[0].x;
    ymin_ = ymax_ = poly.outer[0].y;
    for (const Point& v : poly.outer) {
        xmin_ = std::min(xmin_, v.x); xmax_ = std::max(xmax_, v.x);
        ymin_ = std::min(ymin_, v.y); ymax_ = std::max(ymax_, v.y);
    }
    for (const auto& h : poly.holes) {
        for (const Point& v : h) {
            xmin_ = std::min(xmin_, v.x); xmax_ = std::max(xmax_, v.x);
            ymin_ = std::min(ymin_, v.y); ymax_ = std::max(ymax_, v.y);
        }
    }

    int target = static_cast<int>(
        std::lround(std::sqrt(static_cast<double>(edgeCount_))));
    bandCount_ = std::clamp(target, 1, 4096);
    if (ymax_ == ymin_) bandCount_ = 1;

    using i128 = geo::i128;
    i128 span = static_cast<i128>(ymax_) - ymin_;
    i128 bh = (span + bandCount_ - 1) / bandCount_;
    bandH_ = static_cast<i64>(std::max<i128>(bh, 1));
    y0_ = ymin_;

    for (RingData& rd : rings_) {
        rd.bands.assign(static_cast<size_t>(bandCount_), {});
        for (uint32_t ei = 0; ei < rd.edges.size(); ++ei) {
            const Edge& e = rd.edges[ei];
            i64 lo = std::min(e.a.y, e.b.y);
            i64 hi = std::max(e.a.y, e.b.y);
            if (lo == hi) {
                rd.bands[static_cast<size_t>(bandOf(lo))].push_back(ei);
                continue;
            }
            int b1 = bandOf(lo);
            int b2 = bandOf(hi - 1); // half-open vertical interval
            for (int b = b1; b <= b2; ++b)
                rd.bands[static_cast<size_t>(b)].push_back(ei);
        }
    }
}

int GridIndex::bandOf(i64 y) const {
    using i128 = geo::i128;
    i128 b = (static_cast<i128>(y) - y0_) / bandH_;
    if (b < 0) b = 0;
    if (b >= bandCount_) b = bandCount_ - 1;
    return static_cast<int>(b);
}

size_t GridIndex::totalEdgeSlots() const {
    size_t s = 0;
    for (const RingData& rd : rings_)
        for (const auto& band : rd.bands) s += band.size();
    return s;
}

Location GridIndex::query(const Point& p) const {
    examined_ = 0;
    if (p.x < xmin_ || p.x > xmax_ || p.y < ymin_ || p.y > ymax_)
        return Location::Outside;

    int band = bandOf(p.y);

    auto ringResult = [&](const RingData& rd) -> Location {
        const std::vector<uint32_t>& ids = rd.bands[static_cast<size_t>(band)];
        for (uint32_t ei : ids) {
            const Edge& e = rd.edges[ei];
            ++examined_;
            if (geo::pointOnSegment(p, e.a, e.b)) return Location::Boundary;
        }
        int crossings = 0;
        for (uint32_t ei : ids) {
            const Edge& e = rd.edges[ei];
            bool straddles =
                (e.a.y <= p.y && e.b.y > p.y) ||
                (e.b.y <= p.y && e.a.y > p.y);
            if (!straddles) continue;
            if (geo::orient(e.a, e.b, p) == 0) continue; // support line
            if (geo::rayCrossesRight(p, e.a, e.b)) ++crossings;
        }
        return (crossings & 1) ? Location::Inside : Location::Outside;
    };

    Location outer = ringResult(rings_[0]);
    if (outer != Location::Inside) return outer;
    for (size_t h = 1; h < rings_.size(); ++h) {
        Location hl = ringResult(rings_[h]);
        if (hl == Location::Boundary) return Location::Boundary;
        if (hl == Location::Inside) return Location::Outside;
    }
    return Location::Inside;
}

} // namespace locator
