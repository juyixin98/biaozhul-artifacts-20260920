// meshcheck — offline triangular mesh topology validator.
//
// Usage:
//   meshcheck [options] [request.json]
//
// Reads a JSON request (file argument or stdin), writes the JSON report to
// stdout, diagnostics/progress to stderr. Exit codes:
//   0  report produced and ok == true  (no request or topology errors)
//   1  report produced but topology/request problems were found
//   2  could not read input or parse JSON (no report possible)
#include <cerrno>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "json.hpp"
#include "mesh_check.hpp"

namespace {

void usage(std::ostream& out) {
    out <<
        "Usage: meshcheck [options] [request.json]\n"
        "  Reads JSON mesh request from file or stdin; writes JSON report to stdout.\n"
        "\n"
        "Options:\n"
        "  --eps-abs <float>   Absolute degeneracy tolerance, input units (default 1e-9)\n"
        "  --eps-rel <float>   Tolerance relative to bounding-box diagonal (default 1e-9;\n"
        "                      0 disables coincident-vertex merging)\n"
        "  --compact           Emit compact JSON instead of indented JSON\n"
        "  -h, --help          Show this help\n";
}

} // namespace

int main(int argc, char** argv) {
    meshcheck::Options opts;
    std::string path;
    bool compact = false;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto needArg = [&](double& dst) {
            if (i + 1 >= argc) {
                std::cerr << "meshcheck: option " << a << " requires a value\n";
                return false;
            }
            try {
                dst = std::stod(argv[++i]);
            } catch (...) {
                std::cerr << "meshcheck: invalid number for " << a << ": " << argv[i] << "\n";
                return false;
            }
            return true;
        };
        if (a == "-h" || a == "--help") { usage(std::cout); return 0; }
        else if (a == "--eps-abs") { if (!needArg(opts.epsAbs)) return 2; }
        else if (a == "--eps-rel") { if (!needArg(opts.epsRel)) return 2; }
        else if (a == "--compact") { compact = true; }
        else if (!a.empty() && a[0] == '-') {
            std::cerr << "meshcheck: unknown option: " << a << "\n";
            usage(std::cerr);
            return 2;
        } else if (!path.empty()) {
            std::cerr << "meshcheck: unexpected extra argument: " << a << "\n";
            return 2;
        } else {
            path = a;
        }
    }

    std::string text;
    if (path.empty()) {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        text = ss.str();
    } else {
        std::ifstream in(path);
        if (!in) {
            std::cerr << "meshcheck: cannot open " << path << ": " << std::strerror(errno) << "\n";
            return 2;
        }
        std::ostringstream ss;
        ss << in.rdbuf();
        text = ss.str();
    }

    json::Value request;
    try {
        request = json::parse(text);
    } catch (const json::ParseError& e) {
        std::cerr << "meshcheck: invalid JSON at offset " << e.offset << ": " << e.what() << "\n";
        return 2;
    }
    if (request.type != json::Value::Obj) {
        std::cerr << "meshcheck: request root must be a JSON object\n";
        return 2;
    }

    meshcheck::Report report = meshcheck::runChecks(request, opts);
    json::Value response = meshcheck::buildResponse(report, opts);
    std::cout << json::dump(response, compact ? -1 : 2) << "\n";

    bool hasTopoError = !report.nonManifoldEdges.empty() ||
                        !report.orientationConflicts.empty() ||
                        !report.duplicateFaceIds.empty();
    return (report.requestErrors.empty() && !hasTopoError) ? 0 : 1;
}
