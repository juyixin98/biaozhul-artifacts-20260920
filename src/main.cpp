#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "json.hpp"
#include "scc.hpp"

namespace {

const char* kUsage =
    "Usage: scc_backend [--reference] [--pretty] [input.json]\n"
    "\n"
    "Reads a JSON request (from input.json, or stdin when omitted):\n"
    "  {\"vertices\": <int>, \"edges\": [[u, v], ...]}\n"
    "\n"
    "Writes a JSON response with strongly connected components, per-component\n"
    "cycle witnesses and the deduplicated condensation DAG edges.\n"
    "\n"
    "Options:\n"
    "  --reference  use the naive reachability-matrix reference (n <= "
    "3000)\n"
    "  --pretty     pretty-print the response\n"
    "  --help       show this text\n"
    "\n"
    "Exit codes: 0 success, 2 invalid request, 1 internal/IO error.\n";

std::string readAll(std::istream& in) {
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

int fail(const std::string& message) {
  scc::Json out(scc::Json::Object{{"error", scc::Json(message)},
                                  {"ok", scc::Json(false)}});
  std::cout << out.dump() << "\n";
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  bool useReference = false;
  bool pretty = false;
  std::string inputPath;

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--reference") {
      useReference = true;
    } else if (arg == "--pretty") {
      pretty = true;
    } else if (arg == "--help" || arg == "-h") {
      std::cout << kUsage;
      return 0;
    } else if (!arg.empty() && arg[0] == '-') {
      std::cerr << "unknown option: " << arg << "\n" << kUsage;
      return 2;
    } else if (inputPath.empty()) {
      inputPath = arg;
    } else {
      std::cerr << "multiple input files given\n" << kUsage;
      return 2;
    }
  }

  std::string text;
  if (inputPath.empty()) {
    text = readAll(std::cin);
  } else {
    std::ifstream in(inputPath);
    if (!in) {
      std::cerr << "cannot open input file: " << inputPath << "\n";
      return 1;
    }
    text = readAll(in);
  }

  try {
    const scc::Json req = scc::Json::parse(text);
    const scc::Graph g = scc::graphFromJson(req);
    const scc::SccResult r =
        useReference ? scc::computeSccReference(g) : scc::computeScc(g);
    const scc::Json out = scc::resultToJson(r, g, useReference);
    std::cout << out.dump(pretty ? 2 : -1) << "\n";
    return 0;
  } catch (const scc::JsonError& e) {
    return fail(e.what());
  } catch (const std::exception& e) {
    std::cerr << "internal error: " << e.what() << "\n";
    return 1;
  }
}
