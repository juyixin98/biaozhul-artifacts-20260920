#include "topp/api.hpp"
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

// Offline CLI: reads a request JSON from a file (or stdin with '-'), writes
// the canonical response JSON to stdout. Exit code mirrors HTTP status class:
// 0 = 2xx, 4 = 4xx, 5 = 5xx.
int main(int argc, char** argv) {
    std::string path = "-";
    bool pretty = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--input" && i + 1 < argc) {
            path = argv[++i];
        } else if (a == "--pretty") {
            pretty = true;
        } else if (a == "--help") {
            std::cout << "Usage: topp_cli [request.json | -] [--pretty]\n";
            return 0;
        } else if (!a.empty() && a[0] != '-' && path == "-") {
            path = a;  // first positional argument is the input file
        } else if (a == "-") {
            path = "-";
        } else {
            std::cerr << "unknown argument: " << a << "\n";
            return 2;
        }
    }

    std::stringstream ss;
    if (path == "-") {
        ss << std::cin.rdbuf();
    } else {
        std::ifstream f(path);
        if (!f) {
            std::cerr << "cannot open " << path << "\n";
            return 2;
        }
        ss << f.rdbuf();
    }

    topp::HttpReply reply = topp::handleParameterize(ss.str());
    std::cout << reply.body << (pretty ? "" : "\n");
    if (reply.status >= 500) return 5;
    if (reply.status >= 400) return 4;
    return 0;
}
