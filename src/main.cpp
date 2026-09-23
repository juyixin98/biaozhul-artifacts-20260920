// JSON request entry point for the trajectory interpolation backend.
//
// Usage:
//   trajectory_interp [request.json]     (reads stdin when no file is given)
//
// Request schema (see README.md for the full contract):
//   {
//     "poses": [{"t": 0.0, "position": [x,y,z], "orientation": [w,x,y,z]}, ...],
//     "queries": [0.5, 1.25],
//     "options": {"extrapolation": "clamp"|"error", "max_gap_seconds": 5.0}
//   }
//
// Output (stdout): one JSON object. On any request-level failure the object
// is {"error": {"code": ..., "message": ...}} and the exit code is 2.
// Otherwise {"results": [{"t", "status": "ok", "position", "orientation"} |
//                        {"t", "status": "error", "error": {...}}]} and exit 0.
// Only coordinates and numbers are emitted; no map, no frontend.

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "json.hpp"
#include "trajectory.hpp"

namespace {

struct RequestError : std::runtime_error {
    explicit RequestError(const std::string& m) : std::runtime_error(m) {}
};

double requireNumber(const json::Value& v, const std::string& what) {
    if (!v.isNumber()) throw RequestError(what + " must be a number");
    return v.number;
}

traj::Vec3 parseVec3(const json::Value& v, const std::string& what) {
    if (!v.isArray() || v.arr.size() != 3)
        throw RequestError(what + " must be an array of 3 numbers");
    return {requireNumber(v.arr[0], what), requireNumber(v.arr[1], what),
            requireNumber(v.arr[2], what)};
}

traj::Quat parseQuat(const json::Value& v, const std::string& what) {
    if (!v.isArray() || v.arr.size() != 4)
        throw RequestError(what + " must be an array of 4 numbers [w,x,y,z]");
    return {requireNumber(v.arr[0], what), requireNumber(v.arr[1], what),
            requireNumber(v.arr[2], what), requireNumber(v.arr[3], what)};
}

json::Value vec3ToJson(const traj::Vec3& p) {
    return json::Value::makeArray({json::Value::makeNumber(p.x), json::Value::makeNumber(p.y),
                                   json::Value::makeNumber(p.z)});
}

json::Value quatToJson(const traj::Quat& q) {
    return json::Value::makeArray({json::Value::makeNumber(q.w), json::Value::makeNumber(q.x),
                                   json::Value::makeNumber(q.y), json::Value::makeNumber(q.z)});
}

json::Value errorObject(const std::string& code, const std::string& message) {
    return json::Value::makeObject({{"code", json::Value::makeString(code)},
                                    {"message", json::Value::makeString(message)}});
}

json::Value run(const json::Value& req) {
    if (!req.isObject()) throw RequestError("request must be a JSON object");

    const json::Value* poses = req.find("poses");
    if (!poses || !poses->isArray()) throw RequestError("\"poses\" must be an array");

    std::vector<traj::Pose> keyframes;
    for (size_t i = 0; i < poses->arr.size(); ++i) {
        const json::Value& p = poses->arr[i];
        std::string what = "poses[" + std::to_string(i) + "]";
        if (!p.isObject()) throw RequestError(what + " must be an object");
        const json::Value* t = p.find("t");
        const json::Value* pos = p.find("position");
        const json::Value* ori = p.find("orientation");
        if (!t) throw RequestError(what + ".t is required");
        if (!pos) throw RequestError(what + ".position is required");
        if (!ori) throw RequestError(what + ".orientation is required");
        traj::Pose pose;
        pose.t = requireNumber(*t, what + ".t");
        pose.position = parseVec3(*pos, what + ".position");
        pose.orientation = parseQuat(*ori, what + ".orientation");
        keyframes.push_back(pose);
    }

    traj::Options options;
    if (const json::Value* opts = req.find("options")) {
        if (!opts->isObject()) throw RequestError("\"options\" must be an object");
        if (const json::Value* e = opts->find("extrapolation")) {
            if (!e->isString()) throw RequestError("options.extrapolation must be a string");
            if (e->str == "clamp") {
                options.extrapolation = traj::ExtrapolationPolicy::Clamp;
            } else if (e->str == "error") {
                options.extrapolation = traj::ExtrapolationPolicy::Error;
            } else {
                throw RequestError("options.extrapolation must be \"clamp\" or \"error\"");
            }
        }
        if (const json::Value* g = opts->find("max_gap_seconds")) {
            double gap = requireNumber(*g, "options.max_gap_seconds");
            if (gap <= 0.0) throw RequestError("options.max_gap_seconds must be positive");
            options.max_gap_seconds = gap;
        }
    }

    const json::Value* queries = req.find("queries");
    if (!queries || !queries->isArray()) throw RequestError("\"queries\" must be an array");

    traj::Trajectory trajectory(std::move(keyframes), options);

    json::Array results;
    for (size_t i = 0; i < queries->arr.size(); ++i) {
        double t = requireNumber(queries->arr[i], "queries[" + std::to_string(i) + "]");
        traj::QueryResult r = trajectory.query(t);
        if (r.ok) {
            results.push_back(json::Value::makeObject(
                {{"t", json::Value::makeNumber(t)},
                 {"status", json::Value::makeString("ok")},
                 {"position", vec3ToJson(r.pose.position)},
                 {"orientation", quatToJson(r.pose.orientation)}}));
        } else {
            results.push_back(json::Value::makeObject(
                {{"t", json::Value::makeNumber(t)},
                 {"status", json::Value::makeString("error")},
                 {"error", errorObject(r.error_code, r.error_message)}}));
        }
    }
    return json::Value::makeObject({{"results", json::Value::makeArray(std::move(results))}});
}

}  // namespace

int main(int argc, char** argv) {
    std::string input;
    if (argc > 2) {
        std::cerr << "usage: trajectory_interp [request.json]\n";
        return 64;
    }
    if (argc == 2) {
        std::ifstream f(argv[1]);
        if (!f) {
            std::cerr << "cannot open " << argv[1] << "\n";
            return 66;
        }
        std::ostringstream ss;
        ss << f.rdbuf();
        input = ss.str();
    } else {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        input = ss.str();
    }

    try {
        json::Value request = json::parse(input);
        std::cout << json::serialize(run(request)) << "\n";
        return 0;
    } catch (const json::ParseError& e) {
        std::cout << json::serialize(json::Value::makeObject(
                         {{"error", errorObject("invalid_json", e.what())}}))
                  << "\n";
        return 2;
    } catch (const RequestError& e) {
        std::cout << json::serialize(json::Value::makeObject(
                         {{"error", errorObject("invalid_request", e.what())}}))
                  << "\n";
        return 2;
    } catch (const std::invalid_argument& e) {
        std::cout << json::serialize(json::Value::makeObject(
                         {{"error", errorObject("invalid_trajectory", e.what())}}))
                  << "\n";
        return 2;
    }
}
