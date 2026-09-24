#include "http_server.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cstring>
#include <iostream>
#include <stdexcept>

namespace tf {

namespace {
std::atomic<bool> g_shutdown{false};
extern "C" void shutdownHandler(int) { g_shutdown.store(true); }

constexpr size_t kMaxBodyBytes = 8u * 1024u * 1024u;  // 8 MiB
constexpr size_t kMaxHeaderBytes = 64u * 1024u;

std::string statusText(int code) {
  switch (code) {
    case 200: return "OK";
    case 400: return "Bad Request";
    case 401: return "Unauthorized";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 409: return "Conflict";
    case 411: return "Length Required";
    case 413: return "Payload Too Large";
    case 422: return "Unprocessable Entity";
    case 500: return "Internal Server Error";
    default: return "OK";
  }
}

std::string trim(std::string s) {
  auto issp = [](unsigned char c) { return std::isspace(c) != 0; };
  while (!s.empty() && issp(s.back())) s.pop_back();
  size_t i = 0;
  while (i < s.size() && issp(s[i])) ++i;
  return s.substr(i);
}

// Read until CRLFCRLF or peer close/error.
bool readHeaders(int fd, std::string& all, size_t& header_end) {
  char chunk[2048];
  while (true) {
    auto pos = all.find("\r\n\r\n");
    if (pos != std::string::npos) {
      header_end = pos + 4;
      return true;
    }
    if (all.size() > kMaxHeaderBytes) return false;
    ssize_t r = ::read(fd, chunk, sizeof(chunk));
    if (r == 0) {
      auto p2 = all.find("\r\n\r\n");
      if (p2 != std::string::npos) {
        header_end = p2 + 4;
        return true;
      }
      return false;
    }
    if (r < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    all.append(chunk, static_cast<size_t>(r));
  }
}
}  // namespace

HttpServer::HttpServer(std::string host, int port, Handler handler)
    : host_(std::move(host)),
      port_(port),
      handler_(std::make_shared<Handler>(std::move(handler))) {}

HttpServer::~HttpServer() { stop(); }

void HttpServer::start() {
  listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listen_fd_ < 0)
    throw std::runtime_error(std::string("socket(): ") + std::strerror(errno));

  int yes = 1;
  ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = inet_addr(host_.c_str());
  addr.sin_port = htons(static_cast<uint16_t>(port_));
  if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
    throw std::runtime_error(std::string("bind(): ") + std::strerror(errno));
  if (::listen(listen_fd_, 32) < 0)
    throw std::runtime_error(std::string("listen(): ") + std::strerror(errno));

  socklen_t alen = sizeof(addr);
  ::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&addr), &alen);
  port_ = ntohs(addr.sin_port);

  // Poll-style timeout so shutdown is noticed promptly.
  timeval tv{};
  tv.tv_usec = 250 * 1000;
  ::setsockopt(listen_fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

  running_.store(true);
  accept_thread_ = std::thread([this] { acceptLoop(); });
}

void HttpServer::acceptLoop() {
  while (running_.load() && !g_shutdown.load()) {
    sockaddr_in cli{};
    socklen_t clen = sizeof(cli);
    int fd = ::accept(listen_fd_, reinterpret_cast<sockaddr*>(&cli), &clen);
    if (fd < 0) {
      if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
      if (!running_.load()) break;
      std::cerr << "accept: " << std::strerror(errno) << "\n";
      continue;
    }
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    // A slow/silent client must not pin a worker thread forever.
    timeval rcv{};
    rcv.tv_sec = 5;
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcv, sizeof(rcv));

    // Detach: the captured shared_ptr keeps the handler alive even if the
    // server stops while a request is in flight; serveConnection closes fd.
    std::shared_ptr<Handler> handler_sp = handler_;
    std::thread([fd, handler_sp] { serveConnection(fd, *handler_sp); }).detach();
  }
}

void HttpServer::handleConnection(int fd) { serveConnection(fd, *handler_); }

/*static*/ void HttpServer::serveConnection(int fd, const Handler& handler) {
  auto send_simple = [&](int code, const std::string& message) {
    std::string body =
        "{\"error\":{\"code\":\"HTTP_" + std::to_string(code) +
        "\",\"message\":\"" + message + "\"}}";
    std::string resp = "HTTP/1.1 " + std::to_string(code) + " " +
                       statusText(code) + "\r\n"
                       "Content-Type: application/json\r\n"
                       "Content-Length: " +
                       std::to_string(body.size()) +
                       "\r\nConnection: close\r\n\r\n" + body;
    ::send(fd, resp.data(), resp.size(), MSG_NOSIGNAL);
  };

  std::string raw;
  size_t header_end = 0;
  if (!readHeaders(fd, raw, header_end)) {
    if (!raw.empty()) send_simple(400, "malformed request");
    ::close(fd);
    return;
  }

  HttpRequest req;
  // Request line
  size_t line_end = raw.find("\r\n");
  std::string request_line = raw.substr(0, line_end);
  {
    size_t sp1 = request_line.find(' ');
    size_t sp2 = request_line.rfind(' ');
    if (sp1 == std::string::npos || sp2 == sp1) {
      send_simple(400, "malformed request line");
      ::close(fd);
      return;
    }
    req.method = request_line.substr(0, sp1);
    std::string target = request_line.substr(sp1 + 1, sp2 - sp1 - 1);
    std::string version = request_line.substr(sp2 + 1);
    if (version.compare(0, 5, "HTTP/") != 0) {
      send_simple(400, "unsupported HTTP version");
      ::close(fd);
      return;
    }
    req.raw_target = target;
    size_t qpos = target.find('?');
    if (qpos == std::string::npos) {
      req.target = target;
    } else {
      req.target = target.substr(0, qpos);
      req.query = target.substr(qpos + 1);
    }
  }

  // Headers
  size_t pos = line_end + 2;
  bool has_content_length = false;
  long long content_length = 0;
  bool keep_alive = false;
  while (pos < header_end) {
    size_t eol = raw.find("\r\n", pos);
    if (eol == std::string::npos || eol > header_end) break;
    std::string line = raw.substr(pos, eol - pos);
    pos = eol + 2;
    if (line.empty()) break;
    size_t colon = line.find(':');
    if (colon == std::string::npos) {
      send_simple(400, "malformed header line");
      ::close(fd);
      return;
    }
    std::string key = trim(line.substr(0, colon));
    std::string val = trim(line.substr(colon + 1));
    std::string lk;
    lk.reserve(key.size());
    for (char c : key) lk.push_back(static_cast<char>(std::tolower(c)));
    req.headers[lk] = val;
    if (lk == "content-length") {
      has_content_length = true;
      try {
        content_length = std::stoll(val);
      } catch (...) {
        send_simple(400, "invalid Content-Length");
        ::close(fd);
        return;
      }
    } else if (lk == "transfer-encoding") {
      std::string lv;
      for (char c : val) lv.push_back(static_cast<char>(std::tolower(c)));
      if (lv.find("chunked") != std::string::npos) {
        send_simple(411, "chunked request bodies are not supported");
        ::close(fd);
        return;
      }
    } else if (lk == "connection") {
      std::string lv;
      for (char c : val) lv.push_back(static_cast<char>(std::tolower(c)));
      if (lv.find("keep-alive") != std::string::npos) keep_alive = true;
      if (lv.find("close") != std::string::npos) keep_alive = false;
    }
  }

  if (content_length < 0 ||
      static_cast<unsigned long long>(content_length) > kMaxBodyBytes) {
    send_simple(413, "request body too large");
    ::close(fd);
    return;
  }
  if (has_content_length) {
    req.body = raw.substr(header_end);
    while (static_cast<long long>(req.body.size()) < content_length) {
      char chunk[4096];
      ssize_t r =
          ::read(fd, chunk,
                 std::min<size_t>(sizeof(chunk),
                                  static_cast<size_t>(content_length) -
                                      req.body.size()));
      if (r <= 0) {
        send_simple(400, "truncated request body");
        ::close(fd);
        return;
      }
      req.body.append(chunk, static_cast<size_t>(r));
    }
  } else if (req.method == "POST" || req.method == "PUT") {
    send_simple(411, "Content-Length required");
    ::close(fd);
    return;
  }

  HttpResponse resp;
  try {
    resp = handler(req);
  } catch (const std::exception& ex) {
    resp.status = 500;
    resp.body = std::string("{\"error\":{\"code\":\"INTERNAL\","
                            "\"message\":\"handler exception: ") +
                ex.what() + "\"}}";
  }

  std::string out = "HTTP/1.1 " + std::to_string(resp.status) + " " +
                    statusText(resp.status) + "\r\n"
                    "Content-Type: " +
                    resp.content_type +
                    "\r\n"
                    "Content-Length: " +
                    std::to_string(resp.body.size()) +
                    "\r\nConnection: close\r\n";
  for (const auto& kv : resp.extra_headers)
    out += kv.first + ": " + kv.second + "\r\n";
  out += "\r\n" + resp.body;
  size_t sent = 0;
  while (sent < out.size()) {
    ssize_t w = ::send(fd, out.data() + sent, out.size() - sent, MSG_NOSIGNAL);
    if (w <= 0) break;
    sent += static_cast<size_t>(w);
  }
  (void)keep_alive;  // always close for simplicity and correctness
  ::close(fd);
}

void HttpServer::waitForSignal() {
  struct sigaction sa {};
  sa.sa_handler = shutdownHandler;
  sigemptyset(&sa.sa_mask);
  sigaction(SIGINT, &sa, nullptr);
  sigaction(SIGTERM, &sa, nullptr);
  signal(SIGPIPE, SIG_IGN);
  while (running_.load() && !g_shutdown.load()) {
    struct timespec ts {
      0, 200 * 1000 * 1000
    };
    nanosleep(&ts, nullptr);
  }
  stop();
}

void HttpServer::stop() {
  if (!running_.exchange(false)) return;
  g_shutdown.store(true);
  ::shutdown(listen_fd_, SHUT_RDWR);
  if (accept_thread_.joinable()) accept_thread_.join();
  // Request workers are detached; their SO_RCVTIMEO (5s) bounds their life
  // and their shared_ptr keeps the handler valid until each one returns.
  if (listen_fd_ >= 0) {
    ::close(listen_fd_);
    listen_fd_ = -1;
  }
}

std::string urlDecode(const std::string& s) {
  std::string out;
  out.reserve(s.size());
  auto hex = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  for (size_t i = 0; i < s.size(); ++i) {
    if (s[i] == '%' && i + 2 < s.size()) {
      int hi = hex(s[i + 1]), lo = hex(s[i + 2]);
      if (hi >= 0 && lo >= 0) {
        out.push_back(static_cast<char>(hi * 16 + lo));
        i += 2;
        continue;
      }
    }
    out.push_back(s[i] == '+' ? ' ' : s[i]);
  }
  return out;
}

}  // namespace tf
