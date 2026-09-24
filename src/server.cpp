#include "server.hpp"

#include <algorithm>
#include <arpa/inet.h>
#include <cctype>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#include "protocol.hpp"

namespace pcrs {

namespace {

constexpr size_t kMaxBodyBytes = 16u * 1024u * 1024u;
constexpr int kReadTimeoutSec = 15;
constexpr int kMaxRequestsPerConn = 100;

// Reads until "\r\n\r\n". Returns false on EOF/timeout/error with `closed`
// set true when the peer went away cleanly before sending any data.
bool readHeaders(int fd, std::string& buf, bool& closed) {
    closed = false;
    char chunk[4096];
    while (buf.find("\r\n\r\n") == std::string::npos) {
        ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
        if (n == 0) {
            closed = true;
            return false;
        }
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        buf.append(chunk, static_cast<size_t>(n));
        if (buf.size() > 64u * 1024u) return false;  // headers too large
    }
    return true;
}

bool sendAll(int fd, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        ssize_t n = ::send(fd, data.data() + off, data.size() - off,
                           MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        off += static_cast<size_t>(n);
    }
    return true;
}

std::string statusText(int code) {
    switch (code) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 413: return "Payload Too Large";
        case 500: return "Internal Server Error";
        case 501: return "Not Implemented";
        default: return "Status";
    }
}

}  // namespace

HttpServer::HttpServer(std::string host, int port)
    : host_(std::move(host)), port_(port)) {}

bool HttpServer::start(std::string& errmsg) {
    // Writing to a closed socket must not kill the process.
    ::signal(SIGPIPE, SIG_IGN);

    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        errmsg = std::string("socket(): ") + std::strerror(errno);
        return false;
    }
    int yes = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port_));
    if (host_ == "0.0.0.0" || host_.empty()) {
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
    } else if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
        errmsg = "invalid bind host: " + host_;
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }

    if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) <
        0) {
        errmsg = std::string("bind(): ") + std::strerror(errno);
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    if (::listen(listen_fd_, 64) < 0) {
        errmsg = std::string("listen(): ") + std::strerror(errno);
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    return true;
}

void HttpServer::stop() {
    stopping_ = true;
    if (listen_fd_ >= 0) {
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
}

void HttpServer::handleConnection(int fd) {
    timeval tv{kReadTimeoutSec, 0};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    std::string leftover;
    for (int reqIdx = 0; reqIdx < kMaxRequestsPerConn; ++reqIdx) {
        bool closed = false;
        if (!readHeaders(fd, leftover, closed)) break;

        const size_t headerEnd = leftover.find("\r\n\r\n");
        std::string rawHead = leftover.substr(0, headerEnd);
        std::string body = leftover.substr(headerEnd + 4);
        leftover.clear();
        // Request line.
        const size_t eol = rawHead.find("\r\n");
        const std::string requestLine = rawHead.substr(0, eol);
        std::string method, path, version;
        {
            size_t a = requestLine.find(' ');
            size_t b = requestLine.find(' ', a + 1);
            if (a == std::string::npos || b == std::string::npos) break;
            method = requestLine.substr(0, a);
            path = requestLine.substr(a + 1, b - a - 1);
            version = requestLine.substr(b + 1);
        }

        size_t contentLength = 0;
        bool haveCL = false, chunked = false;
        std::string lowerHeaders;
        for (size_t pos = eol + 2; pos < rawHead.size();) {
            const size_t nl = rawHead.find("\r\n", pos);
            std::string line = rawHead.substr(
                pos, (nl == std::string::npos ? std::string::npos : nl - pos));
            const size_t colon = line.find(':');
            if (colon != std::string::npos) {
                std::string name = line.substr(0, colon);
                std::string val = line.substr(colon + 1);
                for (char& c : name) c = static_cast<char>(::tolower(c));
                size_t s = val.find_first_not_of(" \t");
                size_t e2 = val.find_last_not_of(" \t\r\n");
                val = (s == std::string::npos)
                          ? ""
                          : val.substr(s, e2 - s + 1);
                if (name == "content-length") {
                    haveCL = true;
                    try {
                        contentLength = std::stoull(val);
                    } catch (...) {
                        contentLength = kMaxBodyBytes + 1;
                    }
                } else if (name == "transfer-encoding") {
                    std::string lv = val;
                    for (char& c : lv) c = static_cast<char>(::tolower(c));
                    if (lv.find("chunked") != std::string::npos) chunked = true;
                }
            }
            if (nl == std::string::npos) break;
            pos = nl + 2;
        }

        HttpResponse resp;
        bool keepAlive = true;
        bool bodyComplete = false;
        if (version != "HTTP/1.1" && version != "HTTP/1.0") {
            resp = {505, "application/json",
                    "{\"error\":{\"code\":\"http_version\",\"message\":\"Only "
                    "HTTP/1.0 and HTTP/1.1 are supported.\"}}"};
        } else if (chunked) {
            resp = {501, "application/json",
                    "{\"error\":{\"code\":\"chunked_unsupported\",\"message\":"
                    "\"Send Content-Length framed requests.\"}}"};
        } else if (haveCL && contentLength > kMaxBodyBytes) {
            resp = {413, "application/json",
                    "{\"error\":{\"code\":\"payload_too_large\",\"message\":"
                    "\"Request body exceeds 16 MiB.\"}}"};
        } else {
            if (version == "HTTP/1.0") keepAlive = false;
            // Finish reading the body (Content-Length is the only framing).
            if (haveCL) {
                bool readError = false;
                while (body.size() < contentLength) {
                    char chunk[8192];
                    size_t want =
                        std::min(sizeof(chunk), contentLength - body.size());
                    ssize_t n = ::recv(fd, chunk, want, 0);
                    if (n <= 0) {
                        readError = true;
                        break;
                    }
                    body.append(chunk, static_cast<size_t>(n));
                }
                if (readError) {
                    resp = {400, "application/json",
                            "{\"error\":{\"code\":\"truncated_request\","
                            "\"message\":\"Connection closed before the full "
                            "body was received.\"}}"};
                } else {
                    // Anything pipelined beyond the body stays buffered.
                    leftover = body.substr(contentLength);
                    body.resize(contentLength);
                    bodyComplete = true;
                }
            } else {
                bodyComplete = true;  // empty body
            }
            if (bodyComplete) {
                resp = handleRequest(method, path, body);
            }
        }

        std::string out;
        out += "HTTP/1.1 " + std::to_string(resp.status) + " " +
               statusText(resp.status) + "\r\n";
        out += "Content-Type: " + resp.content_type + "\r\n";
        out += "Content-Length: " + std::to_string(resp.body.size()) + "\r\n";
        out += "Connection: " +
               std::string(keepAlive ? "keep-alive" : "close") + "\r\n";
        out += "Server: pcrs/1.0\r\n";
        out += "\r\n";
        out += resp.body;
        if (!sendAll(fd, out)) break;
        if (!keepAlive) break;
    }
    ::close(fd);
}

void HttpServer::serve() {
    std::vector<std::thread> workers;
    while (!stopping_) {
        sockaddr_in cli{};
        socklen_t clen = sizeof(cli);
        int fd = ::accept(listen_fd_, reinterpret_cast<sockaddr*>(&cli),
                          &clen);
        if (fd < 0) {
            if (stopping_) break;
            if (errno == EINTR) continue;
            continue;
        }
        workers.emplace_back([this, fd] { handleConnection(fd); });
        // Reap finished threads occasionally to bound resource use.
        if (workers.size() % 32 == 0) {
            for (auto& th : workers)
                if (th.joinable()) th.join();
            workers.clear();
        }
    }
    for (auto& th : workers)
        if (th.joinable()) th.join();
}

}  // namespace pcrs
