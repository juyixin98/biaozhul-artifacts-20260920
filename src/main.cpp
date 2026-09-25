// main.cpp — 线段相交离线计算后端的 JSON 请求入口（无任何前端/地图功能）。
//
// 用法：
//   seginter [request.json] [-o response.json] [--pretty]
//     - 省略输入文件时从标准 stdin 读取；省略 -o 时输出到 stdout。
//
// 请求（顶层对象，entries 也可命名为 requests，两种均接受）：
//   {
//     "entries": [
//       { "id": 1,
//         "a": {"x": 0, "y": 0}, "b": {"x": 4, "y": 4},
//         "c": {"x": 0, "y": 4}, "d": {"x": 4, "y": 0} }
//     ]
//   }
//
// 响应：
//   { "results": [
//       { "id": 1, "classification": "cross",
//         "points": [ { "x": {"num": "2", "den": "1"},
//                      "y": {"num": "2", "den": "1"},
//                      "hits": [] } ] } ] }
//
// 坐标超出 [-2^62+2, 2^62-2] 或字段缺失/类型错误时，
// 该条记录返回 {"id": ..., "error": "..."}，其余记录继续计算。
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "geometry.hpp"
#include "json.hpp"

using namespace segint;

namespace {

// 复制 id（字符串或整数，缺省返回 null）。
JVal copy_id(const JVal& e) {
    if (!e.has("id")) return JVal::make_null();
    const JVal& id = e.at("id");
    if (id.type == JVal::String) return JVal::make_string(id.str);
    if (id.type == JVal::Int) return JVal::make_int(id.i);
    throw JError("id must be integer or string");
}

Point parse_point(const JVal& parent, const char* key) {
    const JVal& p = parent.at(key);
    if (p.type != JVal::Object) throw JError(std::string(key) + " must be an object");
    Point pt;
    pt.x = p.at("x").as_int();
    pt.y = p.at("y").as_int();
    if (!coord_in_range(pt.x) || !coord_in_range(pt.y))
        throw JError(std::string(key) + ": coordinate out of range [-2^62+2, 2^62-2]");
    return pt;
}

JVal rational_json(const Rational& r) {
    JVal o = JVal::make_object();
    o.set("num", JVal::make_string(r.num_str()));
    o.set("den", JVal::make_string(r.den_str()));
    return o;
}

JVal one_result(const JVal& entry) {
    JVal id = copy_id(entry);
    JVal out = JVal::make_object();
    out.set("id", id);
    try {
        Point a = parse_point(entry, "a");
        Point b = parse_point(entry, "b");
        Point c = parse_point(entry, "c");
        Point d = parse_point(entry, "d");

        Result r = intersect(a, b, c, d);
        out.set("classification", JVal::make_string(classify_name(r.cls)));
        JVal pts = JVal::make_array();
        for (const ContactPoint& cp : r.points) {
            JVal po = JVal::make_object();
            po.set("x", rational_json(cp.x));
            po.set("y", rational_json(cp.y));
            JVal hits = JVal::make_array();
            for (const std::string& h : cp.hits)
                hits.push(JVal::make_string(h));
            po.set("hits", hits);
            pts.push(po);
        }
        out.set("points", pts);
    } catch (const std::exception& ex) {
        out.set("error", JVal::make_string(ex.what()));
    }
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    std::string in_path;
    std::string out_path;
    bool pretty = false;
    for (int k = 1; k < argc; ++k) {
        std::string arg = argv[k];
        if (arg == "--pretty") {
            pretty = true;
        } else if (arg == "-o" || arg == "--output") {
            if (++k >= argc) {
                std::cerr << "error: " << arg << " requires a file path\n";
                return 2;
            }
            out_path = argv[k];
        } else if (!arg.empty() && arg[0] == '-') {
            std::cerr << "error: unknown option: " << arg << "\n";
            return 2;
        } else {
            in_path = arg;
        }
    }

    std::string text;
    try {
        std::ifstream fin;
        std::istream* in = &std::cin;
        if (!in_path.empty()) {
            fin.open(in_path);
            if (!fin) throw std::runtime_error("cannot open input file: " + in_path);
            in = &fin;
        }
        std::ostringstream ss;
        ss << in->rdbuf();
        text = ss.str();
    } catch (const std::exception& ex) {
        std::cerr << "error: " << ex.what() << "\n";
        return 1;
    }

    JVal response = JVal::make_object();
    JVal results = JVal::make_array();
    try {
        JVal req = json_parse(text);
        if (req.type != JVal::Object) throw JError("top-level value must be an object");
        const JVal* entries = nullptr;
        if (req.has("entries")) entries = &req.at("entries");
        else if (req.has("requests")) entries = &req.at("requests");
        else throw JError("missing 'entries' array");
        if (entries->type != JVal::Array) throw JError("'entries' must be an array");

        for (const JVal& e : entries->as_array()) {
            if (e.type != JVal::Object) {
                JVal bad = JVal::make_object();
                bad.set("error", JVal::make_string("entry must be an object"));
                results.push(bad);
                continue;
            }
            results.push(one_result(e));
        }
        response.set("results", results);
    } catch (const std::exception& ex) {
        JVal err = JVal::make_object();
        err.set("error", JVal::make_string(ex.what()));
        response = err;
    }

    std::string out_text = json_dump(response, pretty ? 2 : -1);
    if (out_path.empty()) {
        std::cout << out_text << '\n';
    } else {
        std::ofstream fout(out_path);
        if (!fout) {
            std::cerr << "error: cannot open output file: " << out_path << "\n";
            return 1;
        }
        fout << out_text << '\n';
    }
    return 0;
}
