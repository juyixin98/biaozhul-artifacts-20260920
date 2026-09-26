// bmatch: bipartite maximum matching with minimum vertex cover certificate.
//
// Usage:
//   bmatch [request.json]     Read a JSON request from the file (or stdin
//                             when omitted / "-") and write the JSON
//                             response to stdout.
//
// Exit codes: 0 = solved (ok:true), 1 = invalid request, 2 = I/O or
// parse failure.
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "api.hpp"
#include "json.hpp"

namespace {

std::string readAll(std::istream& in) {
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

}  // namespace

int main(int argc, char** argv) {
  std::string input;
  if (argc > 2) {
    std::cerr << "usage: bmatch [request.json]\n";
    return 2;
  }
  if (argc == 2 && std::string(argv[1]) != "-") {
    std::ifstream f(argv[1]);
    if (!f) {
      std::cerr << "cannot open file: " << argv[1] << "\n";
      return 2;
    }
    input = readAll(f);
  } else {
    input = readAll(std::cin);
  }

  JsonValue request;
  try {
    request = parseJson(input);
  } catch (const std::exception& ex) {
    JsonValue err = JsonValue::makeObject();
    err.obj.emplace_back("ok", JsonValue::makeBool(false));
    err.obj.emplace_back("error", JsonValue::makeString(ex.what()));
    std::cout << err.dump() << "\n";
    return 2;
  }

  JsonValue response = solveRequest(request);
  std::cout << response.dump() << "\n";
  const JsonValue* ok = response.find("ok");
  return (ok && ok->isBool() && ok->boolean) ? 0 : 1;
}
