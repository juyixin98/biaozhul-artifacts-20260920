// CLI entry point: reads a JSON request (file argument or stdin), solves it
// with the offline segment-tree solver (default) or the naive BFS reference
// (--reference), and writes a JSON response to stdout.
//
// Exit codes: 0 = request processed (check "ok" in the response),
//             2 = usage / I/O / JSON / schema error.
#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "json.hpp"
#include "solver.hpp"

namespace {

std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if ((unsigned char)c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", (unsigned char)c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

void print_usage(const char* prog) {
    std::fprintf(stderr,
                 "usage: %s [--reference] [input.json]\n"
                 "  Reads a JSON request from input.json (or stdin) and writes a JSON\n"
                 "  response to stdout. --reference selects the naive per-query BFS solver.\n",
                 prog);
}

std::string read_stream(std::istream& in) {
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

int fail_schema(const std::string& msg) {
    std::printf("{\"ok\":false,\"error\":\"%s\"}\n", json_escape(msg).c_str());
    return 2;
}

} // namespace

int main(int argc, char** argv) {
    bool reference = false;
    std::string path;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--reference") {
            reference = true;
        } else if (a == "--help" || a == "-h") {
            print_usage(argv[0]);
            return 0;
        } else if (!a.empty() && a[0] == '-') {
            std::fprintf(stderr, "unknown option: %s\n", a.c_str());
            print_usage(argv[0]);
            return 2;
        } else if (path.empty()) {
            path = a;
        } else {
            std::fprintf(stderr, "unexpected argument: %s\n", a.c_str());
            print_usage(argv[0]);
            return 2;
        }
    }

    std::string text;
    if (path.empty()) {
        text = read_stream(std::cin);
    } else {
        std::ifstream f(path);
        if (!f) {
            std::fprintf(stderr, "cannot open file: %s\n", path.c_str());
            return 2;
        }
        text = read_stream(f);
    }

    minijson::Value root;
    try {
        root = minijson::Parser(text).parse();
    } catch (const std::exception& e) {
        std::printf("{\"ok\":false,\"error\":\"invalid JSON: %s\"}\n",
                    json_escape(e.what()).c_str());
        return 2;
    }

    using minijson::Value;
    if (root.type != Value::Type::Object)
        return fail_schema("top-level value must be an object");
    const Value* nv = root.find("n");
    if (!nv || nv->type != Value::Type::Int)
        return fail_schema("field \"n\" must be an integer");
    long n = nv->integer;
    const Value* opsv = root.find("ops");
    if (!opsv || opsv->type != Value::Type::Array)
        return fail_schema("field \"ops\" must be an array");

    std::vector<dynconn::Op> ops;
    ops.reserve(opsv->arr.size());
    for (std::size_t i = 0; i < opsv->arr.size(); ++i) {
        const Value& e = opsv->arr[i];
        std::string where = "ops[" + std::to_string(i) + "]";
        if (e.type != Value::Type::Object)
            return fail_schema(where + " must be an object");
        const Value* t = e.find("type");
        if (!t || t->type != Value::Type::String)
            return fail_schema(where + ".type must be a string");
        dynconn::Op op;
        if (t->str == "add") {
            op.type = dynconn::Op::Type::Add;
        } else if (t->str == "del") {
            op.type = dynconn::Op::Type::Del;
        } else if (t->str == "query") {
            op.type = dynconn::Op::Type::Query;
        } else {
            return fail_schema(where + ".type must be one of \"add\", \"del\", \"query\"");
        }
        const Value* u = e.find("u");
        const Value* v = e.find("v");
        if (!u || u->type != Value::Type::Int || !v || v->type != Value::Type::Int)
            return fail_schema(where + ".u and .v must be integers");
        if (u->integer < -2147483648LL || u->integer > 2147483647LL ||
            v->integer < -2147483648LL || v->integer > 2147483647LL)
            return fail_schema(where + ".u and .v must fit in 32-bit integers");
        op.u = (int)u->integer;
        op.v = (int)v->integer;
        ops.push_back(op);
    }

    dynconn::SolveResult r =
        reference ? dynconn::solve_reference(n, ops) : dynconn::solve_offline(n, ops);

    if (!r.ok) {
        std::printf("{\"ok\":false,\"error\":\"%s\",\"op_index\":%ld}\n",
                    json_escape(r.error).c_str(), r.error_index);
        return 0;
    }
    std::string out = "{\"ok\":true,\"answers\":[";
    for (std::size_t i = 0; i < r.answers.size(); ++i) {
        if (i) out += ',';
        out += r.answers[i] ? "true" : "false";
    }
    out += "]}\n";
    std::fputs(out.c_str(), stdout);
    return 0;
}
