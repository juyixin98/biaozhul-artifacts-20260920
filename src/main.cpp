// CLI entry point for the polygon point-location backend.
//
//   ./poly_locator [request.json]            # file or '-' for stdin
//   cat request.json | ./poly_locator -
//
// Options:
//   --compact      emit compact JSON (default: pretty printed)
//
// Exit code: 0 on a processed request (including a reported invalid polygon),
// 2 on malformed JSON / bad request structure.
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "json.hpp"
#include "service.hpp"

int main(int argc, char** argv) {
    std::string path = "-";
    bool pretty = true;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--compact")
            pretty = false;
        else if (a == "-h" || a == "--help") {
            std::cout << "usage: poly_locator [request.json|-] [--compact]\n";
            return 0;
        } else
            path = a;
    }

    std::stringstream ss;
    if (path == "-") {
        ss << std::cin.rdbuf();
    } else {
        std::ifstream f(path);
        if (!f) {
            std::cerr << "cannot open file: " << path << "\n";
            return 2;
        }
        ss << f.rdbuf();
    }

    Service svc;
    std::string response = svc.handle(ss.str(), pretty);
    std::cout << response;

    // Exit non-zero only for transport-level errors.
    JsonPtr r;
    try {
        r = jsonParse(response);
    } catch (...) {
        return 2;
    }
    if (const JsonPtr* st = r->get("status"))
        if (st->get()->type == JsonType::String &&
            st->get()->str == "error")
            return 2;
    return 0;
}
