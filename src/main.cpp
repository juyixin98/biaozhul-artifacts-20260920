// main.cpp - 多边形裁剪后端的 JSON 请求入口
//
// 用法:
//   polygon-clip                      从 stdin 读取 JSON 请求
//   polygon-clip <request.json>       从文件读取
//   polygon-clip -o <out.json> [req]  将响应写入文件(默认 stdout)
//
// 退出码:
//   0 成功(含空交集/退化结果, status 仍为 ok)
//   1 用法/IO 错误
//   2 JSON 语法错误
//   3 请求结构错误
//   4 几何校验或结果错误(自交、非凸裁剪、结果非简单环等)
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "geometry.hpp"
#include "json.hpp"

using geom::Point;

namespace {

const char* USAGE =
    "usage: polygon-clip [-o OUTPUT] [REQUEST_FILE]\n"
    "  Reads a JSON clip request from REQUEST_FILE or stdin.\n";

std::string readAll(std::istream& in) {
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

[[noreturn]] void emitErrorAndExit(const std::string& code,
                                   const std::string& message, int exitCode,
                                   const std::string& outPath) {
    json::Value root = json::Value::makeObj();
    root.set("status", json::Value::makeStr("error"));
    json::Value e = json::Value::makeObj();
    e.set("code", json::Value::makeStr(code));
    e.set("message", json::Value::makeStr(message));
    root.set("error", e);
    std::string text = json::dump(root);
    if (!outPath.empty()) {
        std::ofstream f(outPath);
        if (!f) {
            std::cerr << "cannot open output file: " << outPath << "\n";
        } else {
            f << text;
        }
    }
    std::cout << text;
    std::exit(exitCode);
}

// 以 [x, y] 数组形式提取一个点; 失败时抛带字段名的异常。
Point extractPoint(const json::Value& v, const char* ringName, size_t idx) {
    if (v.type != json::Type::Array || v.arr.size() != 2)
        throw std::runtime_error(std::string(ringName) + "[" +
                                 std::to_string(idx) +
                                 "] must be a [x, y] pair");
    const json::Value& x = v.arr[0];
    const json::Value& y = v.arr[1];
    if (x.type != json::Type::Number || y.type != json::Type::Number)
        throw std::runtime_error(std::string(ringName) + "[" +
                                 std::to_string(idx) +
                                 "] coordinates must be finite numbers");
    if (!std::isfinite(x.num) || !std::isfinite(y.num))
        throw std::runtime_error(std::string(ringName) + "[" +
                                 std::to_string(idx) +
                                 "] contains non-finite coordinate");
    return {static_cast<long double>(x.num),
            static_cast<long double>(y.num)};
}

std::vector<Point> extractRing(const json::Value& v, const char* name) {
    if (v.type != json::Type::Array)
        throw std::runtime_error(std::string(name) + " must be an array of "
                                                     "[x, y] pairs");
    if (v.arr.size() < 3)
        throw std::runtime_error(std::string(name) +
                                 " must contain at least 3 vertices");
    std::vector<Point> pts;
    pts.reserve(v.arr.size());
    for (size_t i = 0; i < v.arr.size(); ++i)
        pts.push_back(extractPoint(v.arr[i], name, i));
    return pts;
}

std::string formatNum(long double v, int decimalPlaces) {
    double d = static_cast<double>(v);
    char buf[64];
    if (decimalPlaces >= 0)
        std::snprintf(buf, sizeof(buf), "%.*f", decimalPlaces, d);
    else
        std::snprintf(buf, sizeof(buf), "%.17g", d);
    return buf;
}

json::Value pointToJson(const Point& p, int decimalPlaces) {
    json::Value arr = json::Value::makeArr();
    // 数字直接以 Number 输出: 这里借助 strtod 让格式化后的文本仍为合法 JSON 数字。
    arr.arr.push_back(json::Value::makeNum(std::stod(formatNum(p.x, decimalPlaces))));
    arr.arr.push_back(json::Value::makeNum(std::stod(formatNum(p.y, decimalPlaces))));
    return arr;
}

}  // namespace

int main(int argc, char** argv) {
    std::string inPath;
    std::string outPath;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "-h" || a == "--help") {
            std::cout << USAGE;
            return 0;
        }
        if (a == "-o") {
            if (i + 1 >= argc) {
                std::cerr << USAGE;
                return 1;
            }
            outPath = argv[++i];
        } else if (!inPath.empty()) {
            std::cerr << USAGE;
            return 1;
        } else {
            inPath = a;
        }
    }

    std::string text;
    if (inPath.empty() || inPath == "-") {
        text = readAll(std::cin);
    } else {
        std::ifstream f(inPath);
        if (!f) {
            std::cerr << "cannot open request file: " << inPath << "\n";
            return 1;
        }
        text = readAll(f);
    }

    json::ParseResult pr = json::parse(text);
    if (!pr.ok)
        emitErrorAndExit("INVALID_JSON", pr.error, 2, outPath);

    if (pr.value.type != json::Type::Object)
        emitErrorAndExit("INVALID_REQUEST", "request root must be a JSON object",
                         3, outPath);

    const json::Value* subjV = pr.value.find("subject");
    const json::Value* clipV = pr.value.find("clip");
    if (!subjV)
        emitErrorAndExit("INVALID_REQUEST", "missing field \"subject\"", 3,
                         outPath);
    if (!clipV)
        emitErrorAndExit("INVALID_REQUEST", "missing field \"clip\"", 3,
                         outPath);

    std::vector<Point> subject, clip;
    try {
        subject = extractRing(*subjV, "subject");
        clip = extractRing(*clipV, "clip");
    } catch (const std::exception& e) {
        emitErrorAndExit("INVALID_REQUEST", e.what(), 3, outPath);
    }

    geom::Eps eps;
    if (const json::Value* ev = pr.value.find("epsilon")) {
        if (ev->type != json::Type::Number || !std::isfinite(ev->num) ||
            ev->num <= 0 || ev->num > 1.0)
            emitErrorAndExit("INVALID_REQUEST",
                             "\"epsilon\" must be a number in (0, 1]", 3,
                             outPath);
        eps.linear = ev->num;
    }

    int decimalPlaces = -1;
    if (const json::Value* dv = pr.value.find("decimal_places")) {
        if (dv->type != json::Type::Number || !std::isfinite(dv->num) ||
            dv->num < 0 || dv->num > 15 || dv->num != std::floor(dv->num))
            emitErrorAndExit(
                "INVALID_REQUEST",
                "\"decimal_places\" must be an integer in [0, 15]", 3,
                outPath);
        decimalPlaces = static_cast<int>(dv->num);
    }

    geom::ClipResult result;
    std::string err = geom::clipPolygonByConvex(subject, clip, eps, result);
    if (!err.empty()) {
        // err 形如 "CODE: message"。
        std::string code = "GEOMETRY_ERROR";
        std::string msg = err;
        size_t colon = err.find(':');
        if (colon != std::string::npos) {
            code = err.substr(0, colon);
            msg = err.substr(colon + 1);
            if (!msg.empty() && msg[0] == ' ') msg.erase(0, 1);
        }
        emitErrorAndExit(code, msg, 4, outPath);
    }

    // ---- 构造成功响应 ----
    json::Value root = json::Value::makeObj();
    root.set("status", json::Value::makeStr("ok"));

    json::Value metrics = json::Value::makeObj();
    metrics.set("epsilon", json::Value::makeNum(static_cast<double>(eps.linear)));
    metrics.set("area", json::Value::makeNum(static_cast<double>(result.area)));
    metrics.set("signed_area",
                json::Value::makeNum(static_cast<double>(result.area)));
    metrics.set(
        "perimeter",
        [&] {
            long double perim = 0;
            const auto& v = result.vertices;
            if (result.kind == geom::ResultKind::Segment && v.size() == 2) {
                long double dx = v[1].x - v[0].x, dy = v[1].y - v[0].y;
                perim = sqrtl(dx * dx + dy * dy);
            } else if (result.kind == geom::ResultKind::Polygon) {
                for (size_t i = 0; i < v.size(); ++i) {
                    const Point& a = v[i];
                    const Point& b = v[(i + 1) % v.size()];
                    long double dx = b.x - a.x, dy = b.y - a.y;
                    perim += sqrtl(dx * dx + dy * dy);
                }
            }
            return json::Value::makeNum(static_cast<double>(perim));
        }());
    root.set("metrics", metrics);

    json::Value res = json::Value::makeObj();
    res.set("kind", json::Value::makeStr(geom::resultKindName(result.kind)));
    res.set("orientation",
            json::Value::makeStr(result.orientation == 1 ? "CCW" : "NONE"));
    res.set("vertex_count",
            json::Value::makeNum(static_cast<double>(result.vertices.size())));
    json::Value verts = json::Value::makeArr();
    for (const Point& p : result.vertices)
        verts.arr.push_back(pointToJson(p, decimalPlaces));
    res.set("vertices", verts);
    res.set("input_subject_orientation",
            json::Value::makeStr(result.inputSubjectReversed ? "CW" : "CCW"));
    json::Value warnings = json::Value::makeArr();
    if (!result.warning.empty())
        warnings.arr.push_back(json::Value::makeStr(result.warning));
    res.set("warnings", warnings);
    root.set("result", res);

    std::string out = json::dump(root);
    if (!outPath.empty()) {
        std::ofstream f(outPath);
        if (!f) {
            std::cerr << "cannot open output file: " << outPath << "\n";
            return 1;
        }
        f << out;
    }
    std::cout << out;
    return 0;
}
