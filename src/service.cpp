#include "service.hpp"

#include <cctype>
#include <memory>
#include <set>
#include <sstream>

namespace {

struct ReqError {
    std::string code;
    std::string message;
};

std::string expectString(const JsonValue& v, const std::string& ctx) {
    if (v.type == JsonType::String) return v.str;
    throw ReqError{"BAD_REQUEST", ctx + ": expected string"};
}

// Collect every decimal token in one ring (array of [x, y] points).
void collectRingTokens(const JsonValue& ring, std::vector<Decimal>* out) {
    if (ring.type != JsonType::Array)
        throw ReqError{"BAD_REQUEST", "a ring must be an array of points"};
    for (const JsonPtr& pt : ring.arr) {
        if (!pt || pt->type != JsonType::Array || pt->arr.size() < 2)
            throw ReqError{"BAD_REQUEST",
                           "each point must be an array [x, y]"};
        for (int c = 0; c < 2; ++c) {
            const JsonValue& num = *pt->arr[c];
            if (num.type == JsonType::Number)
                out->push_back(parseDecimal(num.numToken));
            else if (num.type == JsonType::String)
                out->push_back(parseDecimal(num.str));
            else
                throw ReqError{"BAD_REQUEST",
                               "coordinates must be JSON numbers or number strings"};
        }
    }
}

void collectQueryTokens(const JsonValue& pts, std::vector<Decimal>* out) {
    if (pts.type != JsonType::Array)
        throw ReqError{"BAD_REQUEST", "'points' must be an array"};
    for (const JsonPtr& pt : pts.arr) {
        if (!pt || pt->type != JsonType::Array || pt->arr.size() < 2)
            throw ReqError{"BAD_REQUEST", "each query point must be [x, y]"};
        for (int c = 0; c < 2; ++c) {
            const JsonValue& num = *pt->arr[c];
            if (num.type == JsonType::Number)
                out->push_back(parseDecimal(num.numToken));
            else if (num.type == JsonType::String)
                out->push_back(parseDecimal(num.str));
            else
                throw ReqError{"BAD_REQUEST",
                               "query coordinates must be numbers or number strings"};
        }
    }
}

Pt readScaledPoint(const JsonValue& pt, long S) {
    Decimal dx = (pt.arr[0]->type == JsonType::Number)
                     ? parseDecimal(pt.arr[0]->numToken)
                     : parseDecimal(pt.arr[0]->str);
    Decimal dy = (pt.arr[1]->type == JsonType::Number)
                     ? parseDecimal(pt.arr[1]->numToken)
                     : parseDecimal(pt.arr[1]->str);
    return Pt{dx.scaled(S), dy.scaled(S)};
}

Ring readRing(const JsonValue& ringNode, long S, bool hole) {
    Ring r;
    r.hole = hole;
    for (const JsonPtr& pt : ringNode.arr)
        r.v.push_back(readScaledPoint(*pt, S));
    return r;
}

JsonPtr numTokenNode(const std::string& token) {
    // Echo the original token; it is a valid JSON number when it came in as a
    // number, and a string when it came in as a string. Prefer number.
    bool numeric = true;
    if (token.empty()) numeric = false;
    for (char c : token)
        if (!(std::isdigit(static_cast<unsigned char>(c)) || c == '-' ||
              c == '+' || c == '.' || c == 'e' || c == 'E'))
            numeric = false;
    return numeric ? JsonValue::makeNumber(token) : JsonValue::makeString(token);
}

std::string exactArea(const Polygon& poly, long S) {
    // Sum of signed doubled areas, then /2 and /10^(2S).
    Int a2 = ringSignedArea2(poly.outer); // >0
    for (const Ring& h : poly.holes) a2 += ringSignedArea2(h); // <0
    Int whole = a2 / 2;
    bool half = ((a2 < 0 ? -a2 : a2) % 2) != 0;
    std::string s = formatScaled(whole, 2 * S);
    if (half) {
        // Add 0.5 in the scaled representation: a2 odd means area = k + 1/2
        // at the 10^(2S) grid, so append "+ 5*10^-(2S+1)" as a finite decimal
        // instead. Represent via a2 directly / (2*10^2S): compute decimal.
        // Simpler: build digits of a2 with one extra fractional digit.
        s = formatScaled(a2, 2 * S + 1); // a2 / 10^(2S+1) == (a2/2)/10^(2S)
    }
    return s;
}

JsonPtr coordNode(const Pt& p, long S) {
    auto arr = JsonValue::makeArray();
    arr->arr.push_back(numTokenNode(formatScaled(p.x, S)));
    arr->arr.push_back(numTokenNode(formatScaled(p.y, S)));
    return arr;
}

JsonPtr bboxNode(const RingIndex& idx, long S) {
    auto o = JsonValue::makeObject();
    o->set("min", coordNode(idx.lo, S));
    o->set("max", coordNode(idx.hi, S));
    return o;
}

} // namespace

JsonPtr Service::handleNode(const JsonValue& root, std::string* errorOut) {
    auto response = JsonValue::makeObject();
    try {
        if (root.type != JsonType::Object)
            throw ReqError{"BAD_REQUEST", "request must be a JSON object"};

        const JsonPtr* opNode = root.get("op");
        std::string op = opNode ? expectString(**opNode, "'op'") : "locate";

        response->set("op", JsonValue::makeString(op));

        const JsonPtr* polyNode = root.get("polygon");
        if (!polyNode)
            throw ReqError{"MISSING_POLYGON", "request requires 'polygon'"};
        const JsonPtr* outerNode = (*polyNode)->get("outer");
        if (!outerNode)
            throw ReqError{"MISSING_OUTER_RING",
                           "'polygon' requires an 'outer' ring"};

        std::vector<Decimal> toks;
        collectRingTokens(**outerNode, &toks);
        const JsonPtr* holesNode = (*polyNode)->get("holes");
        if (holesNode) {
            if ((*holesNode)->type != JsonType::Array)
                throw ReqError{"BAD_REQUEST", "'holes' must be an array"};
            for (const JsonPtr& h : (*holesNode)->arr)
                collectRingTokens(*h, &toks);
        }

        const JsonPtr* pointsNode = root.get("points");
        if (pointsNode) collectQueryTokens(**pointsNode, &toks);

        long S = commonScale(toks);

        auto prepared = std::make_unique<PreparedPolygon>();
        prepared->scale = S;
        prepared->polygon.outer = readRing(**outerNode, S, false);
        if (holesNode)
            for (const JsonPtr& h : (*holesNode)->arr)
                prepared->polygon.holes.push_back(readRing(*h, S, true));

        // Record input orientations from doubled signed areas (before fixing).
        auto orientOf = [](const Ring& r) {
            Int a2 = ringSignedArea2(r);
            if (a2 > 0) return 1;
            if (a2 < 0) return -1;
            return 0;
        };
        prepared->outerInputOrientation.push_back(
            orientOf(prepared->polygon.outer));
        for (const Ring& h : prepared->polygon.holes)
            prepared->holeInputOrientation.push_back(orientOf(h));

        ValidationResult vr = validateAndNormalizeRing(prepared->polygon.outer);
        std::string where = "outer";
        if (vr.ok) {
            for (size_t i = 0; i < prepared->polygon.holes.size(); ++i) {
                vr = validateAndNormalizeRing(prepared->polygon.holes[i]);
                where = "hole " + std::to_string(i);
                if (!vr.ok) break;
            }
        }
        if (vr.ok) {
            vr = validatePolygon(prepared->polygon);
            where = "polygon";
        }
        if (!vr.ok) {
            response->set("status", JsonValue::makeString("invalid_polygon"));
            auto err = JsonValue::makeObject();
            err->set("code", JsonValue::makeString(vr.errorCode));
            err->set("where", JsonValue::makeString(where));
            err->set("message", JsonValue::makeString(vr.message));
            response->set("error", err);
            return response;
        }

        prepared->index.build(prepared->polygon);
        polygons.push_back(std::move(prepared));
        PreparedPolygon& pp = *polygons.back();

        auto meta = JsonValue::makeObject();
        meta->set("coordinate_system", JsonValue::makeString("cartesian_2d"));
        meta->set("arithmetic", JsonValue::makeString("exact_integer_cpp_int"));
        meta->set("common_scale_exponent", JsonValue::makeNumber(std::to_string(S)));
        meta->set("orientation_convention",
                  JsonValue::makeString("outer_ccw_holes_cw"));
        meta->set("outer_input_orientation",
                  JsonValue::makeString(
                      pp.outerInputOrientation[0] > 0 ? "ccw" : "cw"));
        auto holeOri = JsonValue::makeArray();
        for (int o : pp.holeInputOrientation)
            holeOri->arr.push_back(JsonValue::makeString(
                o > 0 ? "ccw" : (o < 0 ? "cw" : "degenerate")));
        meta->set("holes_input_orientation", holeOri);
        response->set("meta", meta);

        auto stats = JsonValue::makeObject();
        stats->set("outer_vertices",
                   JsonValue::makeNumber(
                       std::to_string(pp.polygon.outer.v.size() - 1)));
        stats->set("hole_count",
                   JsonValue::makeNumber(
                       std::to_string(pp.polygon.holes.size())));
        stats->set("area", JsonValue::makeNumber(exactArea(pp.polygon, S)));
        stats->set("outer_bbox", bboxNode(pp.index.outer, S));
        response->set("polygon_stats", stats);

        if (op == "prepare") {
            response->set("status", JsonValue::makeString("ok"));
            response->set("prepared", JsonValue::makeBool(true));
            return response;
        }

        if (op != "locate" && op != "validate" && op != "query")
            throw ReqError{"UNKNOWN_OP", "unknown op: " + op};

        if (op == "validate") {
            response->set("status", JsonValue::makeString("ok"));
            response->set("valid", JsonValue::makeBool(true));
            return response;
        }

        // locate
        if (!pointsNode)
            throw ReqError{"MISSING_POINTS", "locate requires 'points'"};
        bool useCompare = false;
        if (const JsonPtr* cmp = root.get("compare_with_naive"))
            useCompare = (*cmp)->type == JsonType::Bool && (*cmp)->boolean;

        size_t nIn = 0, nOut = 0, nBnd = 0, nMismatch = 0;
        auto results = JsonValue::makeArray();
        for (const JsonPtr& pt : (*pointsNode)->arr) {
            Pt q = readScaledPoint(*pt, S);
            Rel r = point_index::locatePolygon(pp.index, q);
            if (r == Rel::Inside) ++nIn;
            else if (r == Rel::Outside) ++nOut;
            else ++nBnd;

            auto item = JsonValue::makeObject();
            item->set("point", coordNode(q, S));
            item->set("location", JsonValue::makeString(relName(r)));
            if (useCompare) {
                Rel rn = locateNaivePolygon(pp.polygon, q);
                item->set("naive_location", JsonValue::makeString(relName(rn)));
                bool agree = (rn == r);
                item->set("agree", JsonValue::makeBool(agree));
                if (!agree) ++nMismatch;
            }
            results->arr.push_back(item);
        }
        response->set("status", JsonValue::makeString("ok"));
        response->set("results", results);
        auto counts = JsonValue::makeObject();
        counts->set("inside", JsonValue::makeNumber(std::to_string(nIn)));
        counts->set("outside", JsonValue::makeNumber(std::to_string(nOut)));
        counts->set("boundary", JsonValue::makeNumber(std::to_string(nBnd)));
        if (useCompare)
            counts->set("naive_mismatches",
                        JsonValue::makeNumber(std::to_string(nMismatch)));
        response->set("counts", counts);
        return response;
    } catch (const ReqError& e) {
        response = JsonValue::makeObject();
        response->set("status", JsonValue::makeString("error"));
        auto err = JsonValue::makeObject();
        err->set("code", JsonValue::makeString(e.code));
        err->set("message", JsonValue::makeString(e.message));
        response->set("error", err);
        if (errorOut) *errorOut = e.code;
        return response;
    } catch (const std::exception& e) {
        response = JsonValue::makeObject();
        response->set("status", JsonValue::makeString("error"));
        auto err = JsonValue::makeObject();
        err->set("code", JsonValue::makeString("BAD_REQUEST"));
        err->set("message", JsonValue::makeString(e.what()));
        response->set("error", err);
        if (errorOut) *errorOut = "BAD_REQUEST";
        return response;
    }
}

std::string Service::handle(const std::string& requestText, bool pretty) {
    JsonPtr root;
    auto response = JsonValue::makeObject();
    try {
        root = jsonParse(requestText);
    } catch (const std::exception& e) {
        response->set("status", JsonValue::makeString("error"));
        auto err = JsonValue::makeObject();
        err->set("code", JsonValue::makeString("MALFORMED_JSON"));
        err->set("message", JsonValue::makeString(e.what()));
        response->set("error", err);
        return pretty ? jsonDumpPretty(*response) : jsonDump(*response);
    }
    JsonPtr out = handleNode(*root, nullptr);
    return pretty ? jsonDumpPretty(*out) : jsonDump(*out);
}
