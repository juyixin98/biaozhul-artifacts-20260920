// http_server.hpp — minimal dependency-free HTTP/1.1 server (POSIX sockets).
//
// Implements just enough of RFC 7230 for a local JSON API:
//   * request line + headers parsing (Host is not required for 127.0.0.1)
//   * Content-Length request bodies, 411/413 on missing/oversized bodies
//   * persistent connections (Connection: close is honoured)
//   * one thread per connection
// It is a real TCP server speaking real HTTP — no mocking.
#pragma once

#include <atomic>
#include <functional>
#include <string>

namespace jtp {

struct HttpRequest {
  std::string method;
  std::string target;  // path (query string retained)
  std::string body;
};

struct HttpResponse {
  int status = 200;
  std::string content_type = "application/json; charset=utf-8";
  std::string body;
  std::string extra_header;  // optional e.g. "X-Foo: bar\r\n"
};

using HttpHandler = std::function<HttpResponse(const HttpRequest&)>;

class HttpServer {
 public:
  HttpServer(std::string host, int port, HttpHandler handler,
             std::size_t max_body_bytes = 16 * 1024 * 1024);
  ~HttpServer();

  // Binds and starts accepting on a background thread. Returns the bound port
  // (useful when port=0) via bound_port().
  void Start();
  void Stop();
  int bound_port() const { return bound_port_; }

 private:
  void AcceptLoop();
  void HandleConnection(int fd);

  std::string host_;
  int requested_port_;
  int bound_port_ = -1;
  int listen_fd_ = -1;
  std::size_t max_body_bytes_;
  HttpHandler handler_;
  std::atomic<bool> running_{false};
  struct Impl;
  Impl* impl_ = nullptr;  // holds the accept thread
};

}  // namespace jtp
