// Minimal single-threaded HTTP/1.1 server exposing the SAT solver as JSON API.
// Endpoints:
//   GET  /healthz
//   POST /solve   {"num_vars":n,"clauses":[[...]],"node_limit":optional}
//   POST /brute   {"num_vars":n,"clauses":[[...]]}   (naive reference, <=25 vars)
//   POST /verify  {"num_vars":n,"clauses":[[...]],"proof":{...},"status":...}
#ifndef SERVER_HPP
#define SERVER_HPP

#include <string>

namespace sat {

// Runs the server loop on the given port. Returns process exit code.
int runServer(int port);

// Handles one JSON request body for the given path and returns
// (http status code, response body). Exposed for testing.
std::pair<int, std::string> handleRequest(const std::string& method,
                                          const std::string& path,
                                          const std::string& body);

}  // namespace sat

#endif
