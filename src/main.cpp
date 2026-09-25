// SPDX-License-Identifier: MIT
// segi-cli：离线精确线段相交 JSON 入口。
//
// 用法：
//   segi-cli                 从 stdin 读取单个请求 JSON
//   segi-cli -f request.json 从文件读取（支持单对象或 {queries:[...]} 批量）
//   segi-cli --help
//
// 退出码：0 正常（含几何上的 disjoint）；1 输入/JSON/协议错误；2 内部错误。
#include "segi/geometry.hpp"
#include "segi/json.hpp"

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

using namespace segi;

namespace {

struct Fail {
    std::string msg;
    explicit Fail(std::string m) : msg(std::move(m)) {}
};

// 只接受 JSON 整数字面量（拒绝 1.5、1e3 等），并用 BigInt 精确解析
Point parsePoint(const JVal& v) {
    if (!v.is(JType::Object)) throw Fail("point must be an object {x,y}");
    const JVal* xj = v.find("x");
    const JVal* yj = v.find("y");
    if (!xj || !yj) throw Fail("point requires integer fields x and y");
    if (!xj->is(JType::Number) || !yj->is(JType::Number))
        throw Fail("point coordinates must be JSON integers (no fractions/exponents)");
    if (xj->raw.find_first_of(".eE") != std::string::npos ||
        yj->raw.find_first_of(".eE") != std::string::npos)
        throw Fail("point coordinates must be JSON integers (no fractions/exponents)");
    try {
        return Point{BigInt::parse(xj->raw), BigInt::parse(yj->raw)};
    } catch (const std::exception&) {
        throw Fail("invalid integer coordinate");
    }
}

Segment parseSeg(const JVal& v) {
    if (!v.is(JType::Object)) throw Fail("segment must be an object");
    const JVal* pj = v.find("p");
    const JVal* qj = v.find("q");
    if (!pj || !qj) throw Fail("segment requires endpoints p and q");
    return Segment{parsePoint(*pj), parsePoint(*qj)};
}

JVal ratJson(const Rat& r) {
    JVal o = jObj();
    jSet(o, "num", jNum(r.n.to_string()));
    jSet(o, "den", jNum(r.d.to_string()));
    return o;
}

JVal pointJson(const RatPoint& p) {
    JVal o = jObj();
    jSet(o, "x", ratJson(p.x));
    jSet(o, "y", ratJson(p.y));
    return o;
}

JVal resultJson(const IntersectionResult& r) {
    JVal o = jObj();
    jSet(o, "relation", jStr(relName(r.type)));
    jSet(o, "segment_a_zero_length", JVal{JType::Bool, r.aZero, "", "", {}, {}});
    jSet(o, "segment_b_zero_length", JVal{JType::Bool, r.bZero, "", "", {}, {}});

    if (r.type == RelType::CROSS || r.type == RelType::ENDPOINT_TOUCH) {
        jSet(o, "point", pointJson(r.point));
        if (r.type == RelType::ENDPOINT_TOUCH) {
            jSet(o, "contact_on_a", jStr(r.flags.onA));
            jSet(o, "contact_on_b", jStr(r.flags.onB));
        }
    } else if (r.type == RelType::COLLINEAR_OVERLAP) {
        jSet(o, "overlap", [&] {
            JVal ov = jObj();
            jSet(ov, "start", pointJson(r.overlapStart));
            jSet(ov, "end", pointJson(r.overlapEnd));
            return ov;
        }());
    }
    return o;
}

JVal handleOne(const JVal& req) {
    const JVal* aj = req.find("a");
    const JVal* bj = req.find("b");
    if (!aj || !bj) throw Fail("request requires segment fields a and b");
    Segment A = parseSeg(*aj);
    Segment B = parseSeg(*bj);
    return resultJson(classify(A, B));
}

int run(const std::string& text) {
    JVal root;
    try {
        root = jsonParse(text);
    } catch (const JsonError& e) {
        std::cerr << "JSON error: " << e.what() << "\n";
        return 1;
    }

    JVal resp = jObj();
    try {
        if (root.is(JType::Object) && root.find("queries")) {
            const JVal& qs = *root.find("queries");
            if (!qs.is(JType::Array)) throw Fail("queries must be an array");
            JVal results = jArr();
            for (size_t k = 0; k < qs.arr.size(); ++k) {
                try {
                    JVal item = jObj();
                    jSet(item, "index", JVal{JType::Number, false, std::to_string(k), "", {}, {}});
                    jSet(item, "result", handleOne(qs.arr[k]));
                    jPush(results, std::move(item));
                } catch (const Fail& f) {
                    JVal item = jObj();
                    jSet(item, "index", JVal{JType::Number, false, std::to_string(k), "", {}, {}});
                    jSet(item, "error", jStr(f.msg));
                    jPush(results, std::move(item));
                }
            }
            jSet(resp, "status", jStr("ok"));
            jSet(resp, "count", JVal{JType::Number, false, std::to_string(qs.arr.size()), "", {}, {}});
            jSet(resp, "results", std::move(results));
        } else if (root.is(JType::Object) && root.find("a") && root.find("b")) {
            jSet(resp, "status", jStr("ok"));
            jSet(resp, "result", handleOne(root));
        } else {
            throw Fail("request must be {a,b} or {queries:[{a,b},...]}");
        }
    } catch (const Fail& f) {
        JVal err = jObj();
        jSet(err, "status", jStr("error"));
        jSet(err, "error", jStr(f.msg));
        std::cout << jsonDump(err) << "\n";
        return 1;
    }

    std::cout << jsonDump(resp) << "\n";
    return 0;
}

void usage() {
    std::cerr <<
        "segi-cli - exact integer segment intersection\n"
        "Usage:\n"
        "  segi-cli              read one request JSON from stdin\n"
        "  segi-cli -f FILE      read request JSON from FILE (single or batch)\n"
        "  segi-cli --help\n\n"
        "Request: {\"a\":{\"p\":{\"x\":..,\"y\":..},\"q\":{..}}, \"b\":{...}}\n"
        "Coordinates must be JSON integers of arbitrary size.\n";
}

} // namespace

int main(int argc, char** argv) {
    std::string text;
    for (int k = 1; k < argc; ++k) {
        std::string arg = argv[k];
        if (arg == "--help" || arg == "-h") { usage(); return 0; }
        if (arg == "-f") {
            if (k + 1 >= argc) { std::cerr << "missing argument for -f\n"; return 1; }
            std::ifstream in(argv[++k]);
            if (!in) { std::cerr << "cannot open file: " << argv[k] << "\n"; return 1; }
            std::ostringstream ss; ss << in.rdbuf();
            text = ss.str();
        } else {
            std::cerr << "unknown argument: " << arg << "\n";
            usage();
            return 1;
        }
    }
    if (text.empty()) {
        std::ostringstream ss; ss << std::cin.rdbuf();
        text = ss.str();
    }
    try {
        return run(text);
    } catch (const std::exception& e) {
        std::cerr << "internal error: " << e.what() << "\n";
        return 2;
    }
}
