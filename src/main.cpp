// main.cpp — CLI entry: reads one JSON request, writes one JSON response.
//
// Usage:
//   simplify [request.json] [--pretty]
//   cat request.json | simplify            (reads stdin when no file is given)
//
// Exit codes: 0 = ok, 1 = invalid input / runtime error, 2 = bad usage.
// Output is JSON only, on stdout; diagnostics go to stderr.
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "service.hpp"

namespace {

std::string read_all(std::istream& in) {
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

} // namespace

int main(int argc, char** argv) {
    std::string path;
    bool pretty = false;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--pretty") {
            pretty = true;
        } else if (arg == "--help" || arg == "-h") {
            std::cout << "usage: simplify [request.json] [--pretty]\n"
                         "reads a JSON simplification request (file or stdin),\n"
                         "writes a JSON response with the simplified polyline\n"
                         "and a per-point validation report.\n";
            return 0;
        } else if (!arg.empty() && arg[0] == '-') {
            std::cerr << "unknown option: " << arg << "\n";
            return 2;
        } else if (path.empty()) {
            path = arg;
        } else {
            std::cerr << "unexpected extra argument: " << arg << "\n";
            return 2;
        }
    }

    std::string input;
    if (path.empty()) {
        input = read_all(std::cin);
    } else {
        std::ifstream f(path);
        if (!f) {
            std::cerr << "cannot open request file: " << path << "\n";
            return 2;
        }
        input = read_all(f);
    }

    try {
        const simp::Json root = simp::parse_json(input);
        const simp::Request req = simp::parse_request(root);
        const simp::Json out = simp::run_request(req);
        std::cout << simp::json_serialize(out, pretty) << "\n";
        return 0;
    } catch (const simp::JsonError& e) {
        simp::Json err = simp::Json::make_obj();
        err.set("status", simp::Json::make_str("error"));
        err.set("error", simp::Json::make_str(e.what()));
        std::cout << simp::json_serialize(err, pretty) << "\n";
        return 1;
    } catch (const std::exception& e) {
        std::cerr << "internal error: " << e.what() << "\n";
        return 1;
    }
}
