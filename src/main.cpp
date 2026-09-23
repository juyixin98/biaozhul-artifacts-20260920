#include <chrono>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "decimal.hpp"
#include "json.hpp"
#include "locator.hpp"

using json::Value;
using locator::Location;
using locator::Polygon;

namespace {

const char* LOC_STRING(Location l) {
    switch (l) {
        case Location::Inside: return "inside";
        case Location::Outside: return "outside";
        case Location::Boundary: return "boundary";
    }
    return "unknown";
}

Value obj() { return json::makeObject(); }
Value arr() { return json::makeArray(); }
Value str(std::string s) { return json::makeString(std::move(s)); }
Value num(int64_t n) { return json::makeInt(n); }

void fail(std::string code, std::string message) {
    Value e = obj();
    json::set(e, "status", str("error"));
    json::set(e, "error", str(code));
    json::set(e, "message", str(std::move(message)));
    std::cout << json::dumpPretty(e);
    std::exit(2);
}

// ---- raw coordinate collection (numbers kept as text) -------------------

struct RawPoint { std::string x, y; };
struct RawRequest {
    std::vector<RawPoint> polygonPoints; // outer then holes, in order
    std::vector<std::vector<RawPoint>> rings;
    std::vector<RawPoint> queries;
    bool hasPolygon = false;
    bool hasQueries = false;
    std::string method; // "", "naive", "indexed"
};

void requireObject(const Value& v, const std::string& path) {
    if (v.type != json::Type::Object) fail("invalid_request", path + " must be an object");
}

RawPoint readRawPoint(const Value& v, const std::string& path) {
    requireObject(v, path);
    const Value* x = v.find("x");
    const Value* y = v.find("y");
    if (!x || x->type != json::Type::Number)
        fail("invalid_coordinate", path + ".x must be a JSON number");
    if (!y || y->type != json::Type::Number)
        fail("invalid_coordinate", path + ".y must be a JSON number");
    return RawPoint{x->raw, y->raw};
}

RawRequest readRawRequest(const Value& root) {
    requireObject(root, "request root");
    RawRequest rr;

    if (const Value* m = root.find("method")) {
        if (m->type != json::Type::String)
            fail("invalid_request", "method must be a string");
        rr.method = m->str;
        if (rr.method != "naive" && rr.method != "indexed")
            fail("invalid_request", "method must be 'naive' or 'indexed'");
    }

    const Value* polygon = root.find("polygon");
    if (polygon) {
        rr.hasPolygon = true;
        requireObject(*polygon, "polygon");
        const Value* outer = polygon->find("outer");
        if (!outer || outer->type != json::Type::Array || outer->arr.empty())
            fail("invalid_request", "polygon.outer must be a non-empty array");
        const Value* holes = polygon->find("holes");
        if (holes && holes->type != json::Type::Array)
            fail("invalid_request", "polygon.holes must be an array");

        auto readRing = [&](const Value& ringV, const std::string& path) {
            if (ringV.type != json::Type::Array || ringV.arr.size() < 3)
                fail("invalid_request", path + " must be an array of at least 3 points");
            std::vector<RawPoint> rp;
            rp.reserve(ringV.arr.size());
            for (size_t i = 0; i < ringV.arr.size(); ++i)
                rp.push_back(readRawPoint(ringV.arr[i], path + "[" + std::to_string(i) + "]"));
            rr.rings.push_back(rp);
            for (auto& p : rp) rr.polygonPoints.push_back(p);
        };
        readRing(*outer, "polygon.outer");
        if (holes) {
            for (size_t h = 0; h < holes->arr.size(); ++h)
                readRing(holes->arr[h], "polygon.holes[" + std::to_string(h) + "]");
        }
    }

    const Value* queries = root.find("queries");
    if (queries) {
        rr.hasQueries = true;
        if (queries->type != json::Type::Array)
            fail("invalid_request", "queries must be an array");
        rr.queries.reserve(queries->arr.size());
        for (size_t i = 0; i < queries->arr.size(); ++i) {
            const Value& q = queries->arr[i];
            std::string path = "queries[" + std::to_string(i) + "]";
            const Value* pt = q.type == json::Type::Object && q.find("point")
                                  ? q.find("point") : &q;
            if (pt->type != json::Type::Object)
                fail("invalid_request", path + " must be a point object");
            rr.queries.push_back(readRawPoint(*pt, path + ".point"));
        }
    }

    if (!rr.hasPolygon)
        fail("invalid_request", "request must contain a polygon");
    return rr;
}

geo::Point decodePoint(const RawPoint& rp, int scale, const std::string& path) {
    geo::Point p;
    if (!decimal::decode(rp.x, scale, p.x))
        fail("coordinate_out_of_range",
             path + ".x does not fit (|coordinate| <= 4.6e18 after scaling)");
    if (!decimal::decode(rp.y, scale, p.y))
        fail("coordinate_out_of_range",
             path + ".y does not fit (|coordinate| <= 4.6e18 after scaling)");
    return p;
}

const char* RING_KIND_CODE(geo::RingErrorKind k) {
    switch (k) {
        case geo::RingErrorKind::DuplicatePoint: return "duplicate_point";
        case geo::RingErrorKind::TooFewPoints: return "too_few_points";
        case geo::RingErrorKind::DegenerateArea: return "degenerate_ring";
        case geo::RingErrorKind::SelfIntersection: return "self_intersection";
    }
    return "invalid_ring";
}

const char* BUILD_KIND_CODE(locator::BuildErrorKind k) {
    switch (k) {
        case locator::BuildErrorKind::InvalidRing: return "invalid_ring";
        case locator::BuildErrorKind::HoleOutsideOuter: return "hole_outside_outer";
        case locator::BuildErrorKind::HolesIntersectOrNested:
            return "holes_intersect_or_nested";
        case locator::BuildErrorKind::TooManyVertices: return "too_many_vertices";
    }
    return "invalid_polygon";
}

uint64_t nowUs() {
    using namespace std::chrono;
    return static_cast<uint64_t>(
        duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count());
}

} // namespace

int main() {
    std::ostringstream buf;
    buf << std::cin.rdbuf();
    std::string input = buf.str();

    auto pr = json::parse(input);
    if (!pr.ok) fail("invalid_json", pr.error);

    RawRequest rr = readRawRequest(pr.root);

    // Determine the common decimal scale across every coordinate.
    int scale = 0;
    auto scanScale = [&](const RawPoint& rp, const std::string& path) {
        int sx = decimal::requiredScale(rp.x);
        int sy = decimal::requiredScale(rp.y);
        if (sx < 0) fail("invalid_coordinate", path + ".x is not a finite JSON number");
        if (sy < 0) fail("invalid_coordinate", path + ".y is not a finite JSON number");
        scale = std::max(scale, std::max(sx, sy));
        if (scale > decimal::MAX_SCALE)
            fail("coordinate_precision_exceeded",
                 "at most " + std::to_string(decimal::MAX_SCALE) +
                     " decimal places supported (got " + std::to_string(scale) + ")");
    };
    {
        size_t pi = 0;
        for (size_t r = 0; r < rr.rings.size(); ++r) {
            std::string base = r == 0 ? "polygon.outer"
                                      : "polygon.holes[" + std::to_string(r - 1) + "]";
            for (size_t k = 0; k < rr.rings[r].size(); ++k) {
                scanScale(rr.polygonPoints[pi++], base + "[" + std::to_string(k) + "]");
            }
        }
        for (size_t q = 0; q < rr.queries.size(); ++q)
            scanScale(rr.queries[q], "queries[" + std::to_string(q) + "].point");
    }

    // Decode all coordinates with the common scale.
    std::vector<std::vector<geo::Point>> decodedRings;
    decodedRings.reserve(rr.rings.size());
    {
        size_t pi = 0;
        for (size_t r = 0; r < rr.rings.size(); ++r) {
            std::vector<geo::Point> ring;
            ring.reserve(rr.rings[r].size());
            std::string base = r == 0 ? "polygon.outer"
                                      : "polygon.holes[" + std::to_string(r - 1) + "]";
            for (size_t k = 0; k < rr.rings[r].size(); ++k)
                ring.push_back(decodePoint(rr.polygonPoints[pi++], scale,
                                           base + "[" + std::to_string(k) + "]"));
            decodedRings.push_back(std::move(ring));
        }
    }
    std::vector<geo::Point> queryPts;
    queryPts.reserve(rr.queries.size());
    for (size_t q = 0; q < rr.queries.size(); ++q)
        queryPts.push_back(decodePoint(rr.queries[q], scale,
                                       "queries[" + std::to_string(q) + "].point"));

    // Build and validate the polygon (ring checks first, then ring relations).
    Polygon poly;
    locator::BuildError berr;
    std::vector<geo::Point> outer = std::move(decodedRings[0]);
    std::vector<std::vector<geo::Point>> holes(
        std::make_move_iterator(decodedRings.begin() + 1),
        std::make_move_iterator(decodedRings.end()));

    if (!locator::buildPolygon(std::move(outer), std::move(holes), poly, berr)) {
        Value e = obj();
        json::set(e, "status", str("error"));
        json::set(e, "error", str(BUILD_KIND_CODE(berr.kind)));
        json::set(e, "where", str(berr.which));
        json::set(e, "message", str(berr.detail));
        if (berr.kind == locator::BuildErrorKind::InvalidRing) {
            json::set(e, "ring_error", str(RING_KIND_CODE(berr.ringError.kind)));
            if (berr.ringError.edgeIndex >= 0)
                json::set(e, "edge_index", num(berr.ringError.edgeIndex));
        }
        std::cout << json::dumpPretty(e);
        return 1;
    }

    // Validation-only request.
    if (!rr.hasQueries) {
        Value e = obj();
        json::set(e, "status", str("ok"));
        json::set(e, "valid", json::makeBool(true));
        json::set(e, "scale", num(scale));
        json::set(e, "total_vertices", num(static_cast<int64_t>(poly.totalVertices())));
        json::set(e, "holes", num(static_cast<int64_t>(poly.holes.size())));
        std::cout << json::dumpPretty(e);
        return 0;
    }

    bool useIndex = rr.method != "naive"; // default: indexed

    Value meta = obj();
    json::set(meta, "crs", str("Cartesian plane; coordinates are abstract "
                               "dimensionless units, not lon/lat"));
    json::set(meta, "arithmetic", str("exact integer predicates (__int128); "
                                      "input decimals scaled by 10^scale"));
    json::set(meta, "scale", num(scale));
    json::set(meta, "vertex_rule", str(
        "half-open edges: lower endpoint inclusive, upper exclusive; "
        "a ray through a vertex counts a local minimum twice, a maximum "
        "zero times"));

    locator::GridIndex grid;
    uint64_t t0 = nowUs();
    if (useIndex) grid.build(poly);
    uint64_t buildUs = nowUs() - t0;

    Value results = arr();
    uint64_t q0 = nowUs();
    for (size_t i = 0; i < queryPts.size(); ++i) {
        const geo::Point& p = queryPts[i];
        Location loc = useIndex ? grid.query(p) : locator::locateNaive(poly, p);
        Value item = obj();
        Value qpt = obj();
        json::set(qpt, "x", json::makeRawNumber(rr.queries[i].x));
        json::set(qpt, "y", json::makeRawNumber(rr.queries[i].y));
        json::set(item, "query", qpt);
        json::set(item, "location", str(LOC_STRING(loc)));
        json::set(item, "location_code",
                  num(static_cast<int64_t>(loc)));
        json::push(results, item);
    }
    uint64_t queryUs = nowUs() - q0;

    Value stats = obj();
    json::set(stats, "query_count", num(static_cast<int64_t>(queryPts.size())));
    json::set(stats, "index_build_us", num(static_cast<int64_t>(buildUs)));
    json::set(stats, "total_query_us", num(static_cast<int64_t>(queryUs)));
    if (queryPts.size() > 0) {
        json::set(stats, "avg_query_us",
                  json::makeRawNumber(
                      std::to_string(static_cast<double>(queryUs) / queryPts.size())));
    }
    if (useIndex) {
        json::set(stats, "bands", num(grid.bandCount()));
        json::set(stats, "edge_slots", num(static_cast<int64_t>(grid.totalEdgeSlots())));
    }

    Value resp = obj();
    json::set(resp, "status", str("ok"));
    json::set(resp, "method", str(useIndex ? "indexed" : "naive"));
    json::set(resp, "meta", meta);
    json::set(resp, "results", results);
    json::set(resp, "stats", stats);
    std::cout << json::dumpPretty(resp);
    return 0;
}
