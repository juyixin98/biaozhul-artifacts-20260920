// main.cc — CLI entry point for the offline ray/box backend.
//
// Usage:
//   raybox                      read one JSON request from stdin
//   raybox -f <request.json>    read one JSON request from a file
//   echo '<json>' | raybox      equivalent
//
// Output: one JSON document on stdout (exit code 0 for any well-formed
// request, including geometric misses; exit code 2 if the request could not
// even be read).
#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "api.h"

namespace {

void printUsage() {
  std::fprintf(stderr,
               "usage: raybox [-f request.json]\n"
               "       raybox < request.json\n"
               "  Reads one ray/box JSON request and prints one JSON "
               "response.\n");
}

}  // namespace

int main(int argc, char** argv) {
  std::string input;

  if (argc == 1) {
    std::ostringstream ss;
    ss << std::cin.rdbuf();
    input = ss.str();
  } else if (argc == 3 && std::string(argv[1]) == "-f") {
    std::ifstream in(argv[2]);
    if (!in) {
      std::fprintf(stderr, "raybox: cannot open '%s'\n", argv[2]);
      return 2;
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    input = ss.str();
  } else if (argc == 2 &&
             (std::string(argv[1]) == "-h" || std::string(argv[1]) == "--help")) {
    printUsage();
    return 0;
  } else {
    printUsage();
    return 2;
  }

  if (input.empty()) {
    std::fprintf(stderr, "raybox: empty request on input\n");
    return 2;
  }

  std::cout << raybox::handleRequest(input) << '\n';
  return 0;
}
