// Minimal real HTTP/1.1 server over POSIX sockets: one thread per
// connection, parsed request line/headers/body (Content-Length only,
// chunked requests are rejected with 411). No external HTTP dependency.
#pragma once

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace tf {

struct HttpRequest {
  std::string method;
  std::string target;  // path, no query string
  std::string query;
  std::string raw_target;  // original request target including ?query
  std::string body;
  std::map<std::string, std::string> headers;  // lowercase keys

  std::string header(const std::string& key) const {
    auto it = headers.find(key);
    return it == headers.end() ? std::string() : it->second;
  }
};

struct HttpResponse {
  int status = 200;
  std::string content_type = "application/json";
  std::string body;
  std::map<std::string, std::string> extra_headers;
};

using Handler = std::function<HttpResponse(const HttpRequest&)>;

class HttpServer {
 public:
  HttpServer(std::string host, int port, Handler handler);
  ~HttpServer();

  void start();          // binds and listens; returns immediately
  void waitForSignal();  // blocks until SIGINT/SIGTERM, then shuts down
  void stop();
  int port() const { return port_; }

 private:
  void acceptLoop();
  void handleConnection(int fd);
  // Implements one request/response using the supplied handler. Closes fd on
  // completion or transport error.
  static void serveConnection(int fd, const Handler& handler);

  std::string host_;
  int port_;
  int listen_fd_ = -1;
  // Shared with detached worker threads so a handler invocation can never
  // touch a destroyed callable during shutdown.
  std::shared_ptr<Handler> handler_;
  std::atomic<bool> running_{false};
  std::thread accept_thread_;
};

// Parse "application/x-www-form-urlencoded"-style body? Not needed.
// URL-decode helper for query parameters.
std::string urlDecode(const std::string& s);

}  // namespace tf
