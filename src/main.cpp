// main.cpp - CLI entry point for the difference-constraints diagnostic backend.
//
// Usage:
//   diffcon_solver [request.json] [-o response.json]
//
// With no file argument the request JSON is read from stdin. The response JSON
// is written to stdout (or to -o target). The process exits 0 on a well-formed
// request even when the system is infeasible; exit code 2 on malformed requests.
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "solver_service.hpp"

namespace {

std::string readAll(std::istream& in) {
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

void printUsage(std::ostream& out) {
  out <<
      "Difference-constraints diagnostic backend\n"
      "\n"
      "Usage:\n"
      "  diffcon_solver [request.json] [-o response.json]\n"
      "\n"
      "Reads a JSON solve request from the given file or stdin and writes the\n"
      "JSON response to stdout (or to the -o file). Constraint form: x - y <= c.\n"
      "\n"
      "Exit codes: 0 well-formed request (feasible or infeasible),\n"
      "            2 malformed request,\n"
      "            1 usage/IO error.\n";
}

}  // namespace

int main(int argc, char** argv) {
  std::string inputPath;
  std::string outputPath;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "-h") == 0 || std::strcmp(argv[i], "--help") == 0) {
      printUsage(std::cout);
      return 0;
    }
    if (std::strcmp(argv[i], "-o") == 0) {
      if (i + 1 >= argc) {
        std::cerr << "error: -o requires a file path\n";
        return 1;
      }
      outputPath = argv[++i];
    } else if (argv[i][0] == '-') {
      std::cerr << "error: unknown option " << argv[i] << "\n";
      printUsage(std::cerr);
      return 1;
    } else if (inputPath.empty()) {
      inputPath = argv[i];
    } else {
      std::cerr << "error: unexpected argument " << argv[i] << "\n";
      return 1;
    }
  }

  std::string body;
  if (inputPath.empty() || inputPath == "-") {
    body = readAll(std::cin);
  } else {
    std::ifstream in(inputPath);
    if (!in) {
      std::cerr << "error: cannot open input file: " << inputPath << "\n";
      return 1;
    }
    body = readAll(in);
  }

  json::Value response = diffcon::handleSolveRequest(body);
  std::string text = response.dump(2);

  if (outputPath.empty() || outputPath == "-") {
    std::cout << text;
  } else {
    std::ofstream out(outputPath);
    if (!out) {
      std::cerr << "error: cannot open output file: " << outputPath << "\n";
      return 1;
    }
    out << text;
  }

  return response.find("status") && response.find("status")->asString() == "error" ? 2 : 0;
}
