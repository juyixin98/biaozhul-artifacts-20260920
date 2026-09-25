// main.cpp - 多边形裁剪 JSON 请求入口（纯后端，无网络/无前端）
//
// 用法:
//   ./polygon_clip [请求文件.json] [-o 响应文件.json]
//   不给请求文件时从标准输入读取；不给 -o 时输出到标准输出。
//
// 请求 JSON:
// {
//   "subject": [[x, y], ...],   // 简单多边形（自交将被拒绝）
//   "clip":    [[x, y], ...]    // 严格凸多边形
// }
//
// 输出 JSON 见 README。坐标原样视为平面直角坐标，不做投影。
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "geometry.hpp"
#include "json.hpp"

using mini_json::Value;
using geom::Point;
using geom::Ring;

namespace {

std::string read_all(std::istream& in) {
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

bool finite_point(const Point& p) {
    return std::isfinite(p.x) && std::isfinite(p.y);
}

// 从 JSON 值解析点环；失败时填充 err
bool parse_ring(const Value& v, Ring& out, std::string& err) {
    if (!v.is(mini_json::Type::Array)) { err = "必须是 [[x,y], ...] 数组"; return false; }
    for (size_t i = 0; i < v.arr->size(); ++i) {
        const Value& pt = v.arr->at(i);
        if (!pt.is(mini_json::Type::Array) || pt.arr->size() != 2 ||
            !pt.arr->at(0).is(mini_json::Type::Number) ||
            !pt.arr->at(1).is(mini_json::Type::Number)) {
            err = "第 " + std::to_string(i) + " 个点不是 [x, y] 数值对";
            return false;
        }
        Point p{pt.arr->at(0).num, pt.arr->at(1).num};
        if (!finite_point(p)) {
            err = "第 " + std::to_string(i) + " 个点含非有限数值（NaN/Inf）";
            return false;
        }
        out.push_back(p);
    }
    return true;
}

Value points_json(const Ring& r) {
    Value a = Value::make_array();
    for (const auto& p : r) {
        Value q = Value::make_array();
        q.push_back(Value(p.x));
        q.push_back(Value(p.y));
        a.push_back(std::move(q));
    }
    return a;
}

const char* status_name(geom::StatusCode s) {
    switch (s) {
        case geom::StatusCode::OK: return "OK";
        case geom::StatusCode::INVALID_SUBJECT: return "INVALID_SUBJECT";
        case geom::StatusCode::SELF_INTERSECTING: return "SELF_INTERSECTING";
        case geom::StatusCode::INVALID_CLIP: return "INVALID_CLIP";
        case geom::StatusCode::EMPTY_OR_DEGENERATE_INPUT:
            return "EMPTY_OR_DEGENERATE_INPUT";
    }
    return "UNKNOWN";
}

const char* kind_name(geom::ResultKind k) {
    switch (k) {
        case geom::ResultKind::EMPTY: return "EMPTY";
        case geom::ResultKind::POINT: return "POINT";
        case geom::ResultKind::SEGMENT: return "SEGMENT";
        case geom::ResultKind::POLYGON: return "POLYGON";
    }
    return "UNKNOWN";
}

Value error_response(const std::string& code, const std::string& message) {
    Value r = Value::make_object();
    r.set("ok", Value(false));
    r.set("status", Value(code));
    r.set("message", Value(message));
    return r;
}

} // namespace

int main(int argc, char** argv) {
    std::string in_path;
    std::string out_path;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "-o") == 0) {
            if (i + 1 >= argc) {
                std::cerr << "错误: -o 需要一个输出文件参数\n";
                return 2;
            }
            out_path = argv[++i];
        } else if (argv[i][0] == '-') {
            std::cerr << "未知参数: " << argv[i] << "\n";
            return 2;
        } else {
            in_path = argv[i];
        }
    }

    std::string text;
    if (in_path.empty()) {
        text = read_all(std::cin);
    } else {
        std::ifstream f(in_path);
        if (!f) {
            std::cerr << "无法打开输入文件: " << in_path << "\n";
            return 2;
        }
        text = read_all(f);
    }

    Value req;
    try {
        req = mini_json::parse(text);
    } catch (const std::exception& e) {
        Value r = error_response("BAD_JSON", std::string("JSON 解析失败: ") + e.what());
        std::cout << mini_json::dump(r);
        return 1;
    }

    if (!req.is(mini_json::Type::Object)) {
        Value r = error_response("BAD_REQUEST", "请求顶层必须是对象");
        std::cout << mini_json::dump(r);
        return 1;
    }
    const Value* subj_v = req.find("subject");
    const Value* clip_v = req.find("clip");
    if (!subj_v || !clip_v) {
        Value r = error_response("BAD_REQUEST", "缺少 subject 或 clip 字段");
        std::cout << mini_json::dump(r);
        return 1;
    }

    Ring subject, clip;
    std::string err;
    if (!parse_ring(*subj_v, subject, err)) {
        Value r = error_response("INVALID_SUBJECT", "subject: " + err);
        std::cout << mini_json::dump(r);
        return 1;
    }
    if (!parse_ring(*clip_v, clip, err)) {
        Value r = error_response("INVALID_CLIP", "clip: " + err);
        std::cout << mini_json::dump(r);
        return 1;
    }

    geom::Result res = geom::clip_polygon(subject, clip);

    Value root = Value::make_object();
    bool ok = res.status == geom::StatusCode::OK;
    root.set("ok", Value(ok));
    root.set("status", Value(status_name(res.status)));
    root.set("kind", Value(kind_name(res.kind)));
    root.set("message", Value(res.message));

    Value cs = Value::make_object();
    cs.set("type", Value("planar_cartesian"));
    cs.set("axes", Value("x-right, y-up (mathematical)"));
    cs.set("units", Value("as-supplied; no projection performed"));
    root.set("coordinate_system", cs);

    root.set("tolerance", Value(res.tol));
    root.set("vertex_count", Value(static_cast<long long>(res.ring.size())));
    root.set("vertices", points_json(res.ring));
    if (res.kind == geom::ResultKind::POLYGON) {
        root.set("orientation", Value("CCW"));
    } else {
        root.set("orientation", Value("NONE"));
    }
    root.set("area", Value(res.area));
    root.set("signed_area", Value(res.signed_area));

    std::string out_text = mini_json::dump(root);
    if (out_path.empty()) {
        std::cout << out_text;
    } else {
        std::ofstream f(out_path);
        if (!f) {
            std::cerr << "无法写入输出文件: " << out_path << "\n";
            return 2;
        }
        f << out_text;
    }
    return ok ? 0 : 1;
}
