// mincut-backend: read one JSON request (file argument or stdin), write one
// JSON response to stdout. The exit status is 0 even on a well-formed error
// response (the error is in the JSON); it is 2 for malformed input or an
// invalid CLI invocation.

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "json.hpp"
#include "protocol.hpp"

namespace {

int usage() {
  std::cerr << "usage: mincut-backend [request.json]\n"
               "       cat request.json | mincut-backend\n";
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  std::string input;
  if (argc == 1) {
    std::ostringstream ss;
    ss << std::cin.rdbuf();
    input = ss.str();
  } else if (argc == 2) {
    std::ifstream in(argv[1]);
    if (!in) {
      std::cerr << "error: cannot open '" << argv[1] << "'\n";
      return 2;
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    input = ss.str();
  } else {
    return usage();
  }

  mcut::JsonValue request;
  try {
    request = mcut::parse_json(input);
  } catch (const mcut::JsonParseError& e) {
    mcut::JsonValue::Object o;
    o.emplace("status", mcut::JsonValue::string("error"));
    o.emplace("error_code", mcut::JsonValue::string("MALFORMED_JSON"));
    o.emplace("message",
              mcut::JsonValue::string(std::string("JSON parse error at offset ") +
                                      std::to_string(e.offset) + ": " +
                                      e.message));
    std::cout << mcut::dump_json(mcut::JsonValue::object(std::move(o)))
              << '\n';
    return 2;
  }

  mcut::RequestError error;
  mcut::JsonValue response = mcut::handle_request(request, error);
  std::cout << mcut::dump_json(response) << '\n';
  return 0;
}
