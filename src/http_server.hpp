// Minimal single-threaded HTTP/1.1 server backed by POSIX sockets.
// Sufficient for local use: POST /solve with a JSON body. No third-party
// dependencies; request bodies are capped to a fixed maximum.
#pragma once

#include <cstddef>
#include <string>

namespace http {

constexpr size_t kMaxBodyBytes = 16 * 1024 * 1024;  // 16 MiB

struct ServerConfig {
  std::string host = "127.0.0.1";
  int port = 8080;
};

// Runs the accept loop until the process receives SIGINT/SIGTERM.
int runServer(const ServerConfig& config);

}  // namespace http
