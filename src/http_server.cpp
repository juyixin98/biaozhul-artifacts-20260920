#include "http_server.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <sstream>
#include <thread>
#include <vector>

namespace jtp {

struct HttpServer::Impl {
  std::thread accept_thread;
};

namespace {

constexpr int kMaxHeaderBytes = 64 * 1024;

// Read until `need` bytes are accumulated or the peer closes / errors.
bool RecvExact(int fd, std::string& buf, std::size_t need,
               std::size_t hard_cap) {
  while (buf.size() < need) {
    if (buf.size() > hard_cap) return false;
    char chunk[8192];
    const ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
    if (n > 0) {
      buf.append(chunk, static_cast<size_t>(n));
    } else if (n == 0) {
      return false;  // peer closed
    } else if (errno == EINTR) {
      continue;
    } else {
      return false;
    }
  }
  return true;
}

std::string StatusText(int status) {
  switch (status) {
    case 200: return "OK";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 411: return "Length Required";
    case 413: return "Payload Too Large";
    case 422: return "Unprocessable Entity";
    case 500: return "Internal Server Error";
    default:  return "OK";
  }
}

void SendAll(int fd, const std::string& data) {
  size_t off = 0;
  while (off < data.size()) {
    const ssize_t n = ::send(fd, data.data() + off, data.size() - off, 0);
    if (n > 0) {
      off += static_cast<size_t>(n);
    } else if (errno == EINTR) {
      continue;
    } else {
      return;  // give up on this connection
    }
  }
}

}  // namespace

HttpServer::HttpServer(std::string host, int port, HttpHandler handler,
                       std::size_t max_body_bytes)
    : host_(std::move(host)),
      requested_port_(port),
      max_body_bytes_(max_body_bytes),
      handler_(std::move(handler)),
      impl_(new Impl) {}

HttpServer::~HttpServer() {
  Stop();
  delete impl_;
}

void HttpServer::Start() {
  listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listen_fd_ < 0) {
    throw std::runtime_error(std::string("socket(): ") + std::strerror(errno));
  }
  int yes = 1;
  ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = inet_addr(host_.c_str());
  addr.sin_port = htons(static_cast<uint16_t>(requested_port_));
  if (addr.sin_addr.s_addr == INADDR_NONE) {
    throw std::runtime_error("invalid bind address: " + host_);
  }
  if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    const int e = errno;
    ::close(listen_fd_);
    listen_fd_ = -1;
    throw std::runtime_error(std::string("bind(): ") + std::strerror(e));
  }
  if (::listen(listen_fd_, 64) != 0) {
    const int e = errno;
    ::close(listen_fd_);
    listen_fd_ = -1;
    throw std::runtime_error(std::string("listen(): ") + std::strerror(e));
  }
  sockaddr_in actual{};
  socklen_t actual_len = sizeof(actual);
  if (::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&actual),
                    &actual_len) == 0) {
    bound_port_ = ntohs(actual.sin_port);
  }

  // Ignore SIGPIPE from writes to closed sockets; we check send errors too.
  ::signal(SIGPIPE, SIG_IGN);

  running_.store(true);
  impl_->accept_thread = std::thread([this] { AcceptLoop(); });
}

void HttpServer::Stop() {
  if (!running_.exchange(false)) {
    if (listen_fd_ >= 0) {
      ::close(listen_fd_);
      listen_fd_ = -1;
    }
    return;
  }
  if (listen_fd_ >= 0) {
    ::shutdown(listen_fd_, SHUT_RDWR);
    ::close(listen_fd_);
    listen_fd_ = -1;
  }
  if (impl_->accept_thread.joinable()) impl_->accept_thread.join();
}

void HttpServer::AcceptLoop() {
  while (running_.load()) {
    sockaddr_in caddr{};
    socklen_t clen = sizeof(caddr);
    const int cfd =
        ::accept(listen_fd_, reinterpret_cast<sockaddr*>(&caddr), &clen);
    if (cfd < 0) {
      if (!running_.load()) break;
      if (errno == EINTR) continue;
      continue;
    }
    int yes = 1;
    ::setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
    std::thread(&HttpServer::HandleConnection, this, cfd).detach();
  }
}

void HttpServer::HandleConnection(int fd) {
  bool keep_alive = true;
  while (keep_alive && running_.load()) {
    // Read until end of headers.
    std::string raw;
    std::size_t header_end = std::string::npos;
    while (true) {
      header_end = raw.find("\r\n\r\n");
      if (header_end != std::string::npos) break;
      if (raw.size() > static_cast<size_t>(kMaxHeaderBytes)) {
        HttpResponse r;
        r.status = 431;  // Request Header Fields Too Large
        r.body = "{\"ok\":false,\"error\":{\"code\":\"HEADER_TOO_LARGE\","
                 "\"message\":\"request headers exceed 64 KiB\"}}";
        std::string out =
            "HTTP/1.1 431 Request Header Fields Too Large\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Length: " + std::to_string(r.body.size()) +
            "\r\nConnection: close\r\n\r\n" + r.body;
        SendAll(fd, out);
        ::close(fd);
        return;
      }
      char chunk[4096];
      const ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
      if (n > 0) {
        raw.append(chunk, static_cast<size_t>(n));
      } else if (n == 0) {
        ::close(fd);
        return;  // idle keep-alive closed by client
      } else if (errno == EINTR) {
        continue;
      } else {
        ::close(fd);
        return;
      }
    }

    const std::string header_block = raw.substr(0, header_end);
    std::istringstream hs(header_block);
    std::string request_line;
    if (!std::getline(hs, request_line)) {
      ::close(fd);
      return;
    }
    if (!request_line.empty() && request_line.back() == '\r') request_line.pop_back();
    std::istringstream rl(request_line);
    std::string method, target, version;
    rl >> method >> target >> version;
    if (method.empty() || target.empty() ||
        version.find("HTTP/1.") == std::string::npos) {
      const std::string body =
          "{\"ok\":false,\"error\":{\"code\":\"BAD_REQUEST\","
          "\"message\":\"malformed request line\"}}";
      std::string out = "HTTP/1.1 400 Bad Request\r\nContent-Type: "
                        "application/json; charset=utf-8\r\nContent-Length: " +
                        std::to_string(body.size()) +
                        "\r\nConnection: close\r\n\r\n" + body;
      SendAll(fd, out);
      ::close(fd);
      return;
    }

    std::string line;
    std::string content_length_str;
    bool has_cl = false;
    std::string connection_header;
    while (std::getline(hs, line)) {
      if (!line.empty() && line.back() == '\r') line.pop_back();
      if (line.empty()) break;
      const auto colon = line.find(':');
      if (colon == std::string::npos) continue;
      std::string name = line.substr(0, colon);
      std::string value = line.substr(colon + 1);
      while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
        value.erase(value.begin());
      for (auto& c : name) c = static_cast<char>(std::tolower(c));
      if (name == "content-length") {
        content_length_str = value;
        has_cl = true;
      } else if (name == "connection") {
        connection_header = value;
        for (auto& c : connection_header) c = static_cast<char>(std::tolower(c));
      }
    }
    if (connection_header.find("close") != std::string::npos ||
        version == "HTTP/1.0") {
      keep_alive = false;
    }

    // Body handling.
    std::string body = raw.substr(header_end + 4);
    auto reply_error = [&](int status, const char* code, const char* msg) {
      HttpResponse r;
      r.status = status;
      r.body = std::string("{\"ok\":false,\"error\":{\"code\":\"") + code +
               "\",\"message\":\"" + msg + "\"}}";
      std::string head = "HTTP/1.1 " + std::to_string(status) + " " +
                         StatusText(status) +
                         "\r\nContent-Type: application/json; charset=utf-8\r\n"
                         "Content-Length: " +
                         std::to_string(r.body.size()) +
                         (keep_alive ? "\r\nConnection: keep-alive"
                                     : "\r\nConnection: close") +
                         "\r\n\r\n";
      SendAll(fd, head + r.body);
    };

    if (method == "POST" || method == "PUT") {
      if (!has_cl) {
        reply_error(411, "LENGTH_REQUIRED",
                    "Content-Length is required for request bodies");
        ::close(fd);
        return;
      }
      long long cl = -1;
      try {
        cl = std::stoll(content_length_str);
      } catch (...) {
        reply_error(400, "BAD_REQUEST", "invalid Content-Length");
        ::close(fd);
        return;
      }
      if (cl < 0) {
        reply_error(400, "BAD_REQUEST", "negative Content-Length");
        ::close(fd);
        return;
      }
      if (static_cast<std::size_t>(cl) > max_body_bytes_) {
        reply_error(413, "PAYLOAD_TOO_LARGE", "request body exceeds limit");
        ::close(fd);
        return;
      }
      if (!RecvExact(fd, body, static_cast<std::size_t>(cl),
                     max_body_bytes_ + kMaxHeaderBytes)) {
        ::close(fd);
        return;
      }
      body.resize(static_cast<size_t>(cl));
    }

    HttpRequest req;
    req.method = method;
    req.target = target;
    req.body = body;

    HttpResponse resp;
    try {
      resp = handler_(req);
    } catch (const std::exception& e) {
      resp.status = 500;
      resp.body = std::string("{\"ok\":false,\"error\":{\"code\":"
                              "\"INTERNAL_ERROR\",\"message\":\"") +
                  e.what() + "\"}}";
    } catch (...) {
      resp.status = 500;
      resp.body = "{\"ok\":false,\"error\":{\"code\":\"INTERNAL_ERROR\","
                  "\"message\":\"unknown exception\"}}";
    }

    std::ostringstream out;
    out << "HTTP/1.1 " << resp.status << ' ' << StatusText(resp.status) << "\r\n"
        << "Content-Type: " << resp.content_type << "\r\n"
        << "Content-Length: " << resp.body.size() << "\r\n"
        << (keep_alive ? "Connection: keep-alive" : "Connection: close") << "\r\n"
        << resp.extra_header;
    SendAll(fd, out.str());
    SendAll(fd, "\r\n");
    SendAll(fd, resp.body);
  }
  ::close(fd);
}

}  // namespace jtp
