// Entry point.
//
// Modes:
//   bipartite-matching                      read one JSON request (or a
//                                           JSON array of requests) from
//                                           stdin, print response(s)
//   bipartite-matching --file req.json ...  process one or more files;
//                                           JSON Lines output
//   bipartite-matching serve [--host H] [--port N]
//                                           start the HTTP service
//   bipartite-matching --help
//   bipartite-matching --version
#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "http_server.hpp"
#include "json.hpp"
#include "protocol.hpp"

namespace {

constexpr const char* kVersion = "1.0.0";

int printUsage() {
  std::fprintf(stderr,
               "bipartite-matching %s\n\n"
               "Usage:\n"
               "  bipartite-matching                     Read JSON request "
               "from stdin\n"
               "  bipartite-matching --file FILE ...     Process request "
               "files (JSON Lines output)\n"
               "  bipartite-matching serve [--host H] [--port N]\n"
               "                                         Start HTTP service "
               "(default 127.0.0.1:8080)\n"
               "  bipartite-matching --help | --version\n",
               kVersion);
  return 2;
}

std::string readAll(std::istream& input) {
  std::ostringstream buffer;
  buffer << input.rdbuf();
  return buffer.str();
}

int runJson(const std::string& rawJson) {
  json::Value parsed;
  try {
    parsed = json::parse(rawJson);
  } catch (const json::ParseError& error) {
    json::Value root = json::Value::makeObject();
    root.members.emplace_back("ok", json::Value::makeBoolean(false));
    json::Value errorJson = json::Value::makeObject();
    errorJson.members.emplace_back("code",
                                   json::Value::makeString("INVALID_JSON"));
    errorJson.members.emplace_back("message",
                                   json::Value::makeString(error.what()));
    root.members.emplace_back("error", errorJson);
    std::cout << json::dump(root) << '\n';
    return 1;
  }

  if (parsed.isArray()) {
    json::Value results = json::Value::makeArray();
    int failures = 0;
    for (const json::Value& request : parsed.items) {
      json::Value response = protocol::handleRequest(request);
      if (!response.find("ok") || !response.find("ok")->boolean) {
        ++failures;
      }
      results.items.push_back(response);
    }
    std::cout << json::dump(results) << '\n';
    return failures == 0 ? 0 : 1;
  }

  json::Value response = protocol::handleRequest(parsed);
  std::cout << json::dump(response) << '\n';
  const json::Value* ok = response.find("ok");
  return (ok != nullptr && ok->boolean) ? 0 : 1;
}

int runFile(const std::string& path) {
  std::ifstream input(path);
  if (!input) {
    std::fprintf(stderr, "cannot open file: %s\n", path.c_str());
    return 2;
  }
  std::string rawJson = readAll(input);

  json::Value parsed;
  json::Value envelope = json::Value::makeObject();
  envelope.members.emplace_back("file", json::Value::makeString(path));
  try {
    parsed = json::parse(rawJson);
  } catch (const json::ParseError& error) {
    json::Value errorResponse = json::Value::makeObject();
    errorResponse.members.emplace_back("ok",
                                       json::Value::makeBoolean(false));
    json::Value errorJson = json::Value::makeObject();
    errorJson.members.emplace_back("code",
                                   json::Value::makeString("INVALID_JSON"));
    errorJson.members.emplace_back("message",
                                   json::Value::makeString(error.what()));
    errorResponse.members.emplace_back("error", errorJson);
    envelope.members.emplace_back("response", errorResponse);
    std::cout << json::dump(envelope, -1) << '\n';
    return 1;
  }

  // A single file holds exactly one request (no in-file batches), so the
  // whole envelope stays on one JSON Line.
  if (parsed.isArray()) {
    std::fprintf(stderr,
                 "%s: batch arrays are supported via stdin, not --file\n",
                 path.c_str());
    return 2;
  }
  envelope.members.emplace_back("response",
                                protocol::handleRequest(parsed));
  std::cout << json::dump(envelope, -1) << '\n';
  const json::Value* response = envelope.find("response");
  const json::Value* ok = response == nullptr ? nullptr : response->find("ok");
  return (ok != nullptr && ok->boolean) ? 0 : 1;
}

int runServe(int argc, char** argv) {
  http::ServerConfig config;
  for (int i = 0; i < argc; ++i) {
    std::string argument = argv[i];
    if (argument == "--host" && i + 1 < argc) {
      config.host = argv[++i];
    } else if (argument == "--port" && i + 1 < argc) {
      config.port = std::stoi(argv[++i]);
    } else {
      std::fprintf(stderr, "unknown serve option: %s\n", argument.c_str());
      return 2;
    }
  }
  return http::runServer(config);
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 1) {
    return runJson(readAll(std::cin));
  }

  std::string command = argv[1];
  if (command == "--help" || command == "-h") {
    printUsage();
    return 0;
  }
  if (command == "--version" || command == "-v") {
    std::printf("%s\n", kVersion);
    return 0;
  }
  if (command == "serve") {
    return runServe(argc - 2, argv + 2);
  }
  if (command == "--file") {
    if (argc < 3) {
      std::fprintf(stderr, "--file requires at least one path\n");
      return 2;
    }
    int worstCode = 0;
    for (int i = 2; i < argc; ++i) {
      int code = runFile(argv[i]);
      if (code > worstCode) worstCode = code;
    }
    return worstCode;
  }

  std::fprintf(stderr, "unknown argument: %s\n", command.c_str());
  return printUsage();
}
