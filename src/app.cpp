//
// app.cpp — request validation and metric orchestration.
//
#include <algorithm>
#include <cstdint>
#include <limits>

#include "app.hpp"

namespace ru::app {

namespace {

namespace j = ru::json;

std::string i128AbsToString(ru::i128 v) {
    if (v == 0) return "0";
    std::string s;
    while (v > 0) {
        s.push_back(static_cast<char>('0' + static_cast<int>(v % 10)));
        v /= 10;
    }
    std::reverse(s.begin(), s.end());
    return s;
}

std::string errorBody(const std::string& code, const std::string& message) {
    j::Value root = j::Value::object();
    root.set("ok", j::Value(false));
    root.set("error", [&] {
        j::Value e = j::Value::object();
        e.set("code", j::Value(code));
        e.set("message", j::Value(message));
        return e;
    }());
    return j::dump(root);
}

// Return a field error message or empty string.
std::string readCoord(const j::Value& rect, const char* name, ru::i64& out) {
    if (!rect.has(name)) return std::string("missing field \"") + name + "\"";
    const j::Value& f = rect.at(name);
    if (f.isNull()) return std::string("field \"") + name + "\" must be an integer";
    if (!f.isNumber()) return std::string("field \"") + name + "\" must be an integer";
    if (!f.isInt()) return std::string("field \"") + name + "\" must be an integer (fractions not allowed)";
    long long v = f.asInt();
    if (v < -COORD_LIMIT || v > COORD_LIMIT)
        return std::string("field \"") + name + "\" out of range [" +
               std::to_string(-COORD_LIMIT) + ", " + std::to_string(COORD_LIMIT) + "]";
    out = v;
    return "";
}

}  // namespace

std::string i128ToString(ru::i128 v) {
    if (v < 0) return "-" + i128AbsToString(-v);
    return i128AbsToString(v);
}

Response processRequest(const std::string& payload) {
    j::Value root;
    try {
        root = j::parse(payload);
    } catch (const std::exception& ex) {
        return {400, errorBody("INVALID_JSON", std::string("request body is not valid JSON: ") + ex.what())};
    }

    if (!root.isObject()) return {400, errorBody("INVALID_REQUEST", "request must be a JSON object")};
    if (!root.has("rectangles"))
        return {400, errorBody("INVALID_REQUEST", "missing field \"rectangles\"")};
    const j::Value& arr = root.at("rectangles");
    if (!arr.isArray())
        return {400, errorBody("INVALID_REQUEST", "\"rectangles\" must be an array")};
    if (arr.arr().size() > 1'000'000)
        return {400, errorBody("TOO_MANY_RECTANGLES", "at most 1,000,000 rectangles per request")};

    std::vector<ru::Rect> valid;
    j::Value ignored = j::Value::array();

    for (size_t idx = 0; idx < arr.arr().size(); ++idx) {
        const j::Value& item = arr.arr()[idx];
        if (!item.isObject())
            return {400, errorBody("INVALID_RECTANGLE",
                                   "rectangle at index " + std::to_string(idx) + " must be an object")};
        ru::Rect r;
        std::string err;
        if ((err = readCoord(item, "x1", r.x1)).size() ||
            (err = readCoord(item, "y1", r.y1)).size() ||
            (err = readCoord(item, "x2", r.x2)).size() ||
            (err = readCoord(item, "y2", r.y2)).size()) {
            return {400, errorBody("INVALID_RECTANGLE",
                                   "rectangle at index " + std::to_string(idx) + ": " + err)};
        }

        // Half-open empty boxes (zero width or height) carry no area and no
        // boundary; skip them and record where they were.
        if (r.x1 == r.x2 || r.y1 == r.y2) {
            j::Value entry = j::Value::object();
            entry.set("index", j::Value(static_cast<long long>(idx)));
            if (item.has("id") && item.at("id").isString())
                entry.set("id", item.at("id"));
            entry.set("reason", j::Value("zero-area rectangle has empty half-open interior"));
            ignored.push(std::move(entry));
            continue;
        }

        // Strict geometry: x1>x2 or y1>y2 is a malformed box, not a normalization.
        if (r.x1 > r.x2 || r.y1 > r.y2)
            return {400, errorBody("INVALID_RECTANGLE",
                                   "rectangle at index " + std::to_string(idx) +
                                   ": expected x1 <= x2 and y1 <= y2 (half-open [x1,x2) x [y1,y2))")};
        valid.push_back(r);
    }

    ru::Metrics m = ru::computeUnionMetrics(valid);

    // Output is serialized as a decimal integer; int64 is the documented
    // output range given the input bounds.
    if (m.area > std::numeric_limits<int64_t>::max() ||
        m.perimeter > std::numeric_limits<int64_t>::max()) {
        return {422, errorBody("RESULT_OVERFLOW",
                               "computed metric exceeds int64 range; reduce input extent")};
    }

    j::Value out = j::Value::object();
    out.set("ok", j::Value(true));
    out.set("area", j::Value(static_cast<long long>(m.area)));
    out.set("perimeter", j::Value(static_cast<long long>(m.perimeter)));
    j::Value units = j::Value::object();
    units.set("coordinate_system", j::Value("cartesian, y-up, integer lattice"));
    units.set("area_unit", j::Value("square grid units"));
    units.set("length_unit", j::Value("grid units"));
    units.set("semantics", j::Value("half-open [x1,x2) x [y1,y2)"));
    out.set("units", units);
    out.set("rectangles_in", j::Value(static_cast<long long>(arr.arr().size())));
    out.set("rectangles_used", j::Value(static_cast<long long>(valid.size())));
    out.set("ignored_rectangles", ignored);

    return {200, j::dump(out)};
}

}  // namespace ru::app
