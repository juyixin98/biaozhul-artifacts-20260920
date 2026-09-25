// Command-line JSON interface for the dominator service.
//
// Usage:
//   domtree_backend                 read JSON request from stdin
//   domtree_backend -f request.json read JSON request from a file
//   domtree_backend --help
//
// Exit code: 0 on a well-formed request (even if success=false due to
// application-level validation, e.g. an unknown query node); 2 when the
// request file cannot be opened or stdin cannot be read.
#include <fstream>
#include <iostream>
#include <iterator>
#include <sstream>
#include <string>

#include "api.hpp"

namespace {

void print_help() {
  std::cerr <<
      "domtree_backend - immediate dominator tree / dominance frontier\n"
      "\n"
      "Usage:\n"
      "  domtree_backend                 read JSON request from stdin\n"
      "  domtree_backend -f <file.json>  read JSON request from a file\n"
      "  domtree_backend --help          show this help\n"
      "\n"
      "The JSON response is written to stdout. See README.md for the\n"
      "request schema and tests/requests/ for examples.\n";
}

}  // namespace

int main(int argc, char** argv) {
  std::string request_text;

  if (argc == 1) {
    std::cin >> std::noskipws;
    request_text.assign(std::istream_iterator<char>(std::cin),
                        std::istream_iterator<char>());
    if (!std::cin.good() && !std::cin.eof()) {
      std::cerr << "error: failed to read request from stdin\n";
      return 2;
    }
  } else if (argc == 3 && std::string(argv[1]) == "-f") {
    std::ifstream in(argv[2]);
    if (!in) {
      std::cerr << "error: cannot open request file: " << argv[2] << "\n";
      return 2;
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    request_text = ss.str();
  } else if (argc == 2 &&
             (std::string(argv[1]) == "--help" ||
              std::string(argv[1]) == "-h")) {
    print_help();
    return 0;
  } else {
    std::cerr << "error: invalid arguments\n\n";
    print_help();
    return 2;
  }

  std::cout << domtree::handle_request(request_text);
  return 0;
}
