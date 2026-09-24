// http_server.cpp —— 基于 POSIX socket 的极简 HTTP/1.1（一线程一连接，keep-alive）
#include "http_server.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>

namespace http {

const char* statusText(int code) {
    switch (code) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 409: return "Conflict";
        case 413: return "Payload Too Large";
        case 422: return "Unprocessable Entity";
        case 500: return "Internal Server Error";
        default:  return "OK";
    }
}

namespace {

constexpr std::size_t kMaxHeaderBytes = 64 * 1024;
constexpr std::size_t kMaxBodyBytes = 16 * 1024 * 1024;

// 从 fd 读取直到遇到 '\n'（先消费缓冲 buf[pos..]），返回不含 CRLF 的一行。
bool readLine(int fd, std::string& buf, std::size_t& pos, std::string& out) {
    while (true) {
        auto nl = buf.find('\n', pos);
        if (nl != std::string::npos) {
            out.assign(buf, pos, nl - pos);
            pos = nl + 1;
            if (!out.empty() && out.back() == '\r') out.pop_back();
            return true;
        }
        char tmp[4096];
        ssize_t n = ::read(fd, tmp, sizeof(tmp));
        if (n <= 0) return false;
        buf.append(tmp, std::size_t(n));
    }
}

// 从 fd 恰好读取 n 字节 body。
bool readN(int fd, std::string& buf, std::size_t& pos, std::size_t n,
           std::string& out) {
    while (buf.size() - pos < n) {
        char tmp[8192];
        std::size_t have = buf.size() - pos;
        std::size_t want = std::min<std::size_t>(sizeof(tmp), n - have);
        ssize_t r = ::read(fd, tmp, want);
        if (r <= 0) return false;
        buf.append(tmp, std::size_t(r));
    }
    out.assign(buf, pos, n);
    pos += n;
    return true;
}

std::string lower(const std::string& s) {
    std::string r;
    r.reserve(s.size());
    for (char c : s)
        r.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    return r;
}

void writeAll(int fd, const std::string& s) {
    const char* p = s.data();
    std::size_t left = s.size();
    while (left > 0) {
        ssize_t w = ::write(fd, p, left);
        if (w <= 0) {
            if (errno == EINTR) continue;
            return;
        }
        p += w;
        left -= std::size_t(w);
    }
}

void sendSimple(int fd, int status, const std::string& json,
                bool close) {
    std::string h = "HTTP/1.1 " + std::to_string(status) + " " +
                    statusText(status) + "\r\n";
    h += "Content-Type: application/json\r\nContent-Length: " +
         std::to_string(json.size()) + "\r\nConnection: " +
         (close ? "close" : "keep-alive") + "\r\n\r\n";
    writeAll(fd, h);
    writeAll(fd, json);
}

void handleConnection(int fd, const Handler& handler) {
    std::string buf;
    std::size_t pos = 0;
    bool clientClose = false;

    while (!clientClose) {
        std::string requestLine;
        if (!readLine(fd, buf, pos, requestLine)) break;
        if (requestLine.empty()) break;  // 试探空行：直接关连接

        std::istringstream iss(requestLine);
        std::string method, target, version;
        iss >> method >> target >> version;
        if (method.empty() || target.empty()) break;
        std::string path = target;
        if (auto q = path.find('?'); q != std::string::npos) path.erase(q);

        std::size_t contentLength = 0;
        while (true) {
            std::string line;
            if (!readLine(fd, buf, pos, line)) goto done;
            if (buf.size() - pos > kMaxHeaderBytes) {
                sendSimple(fd, 400,
                           "{\"error\":\"headers_too_large\"}", true);
                goto done;
            }
            if (line.empty()) break;
            auto colon = line.find(':');
            if (colon == std::string::npos) continue;
            std::string name = lower(line.substr(0, colon));
            std::string val = line.substr(colon + 1);
            while (!val.empty() &&
                   (val.front() == ' ' || val.front() == '\t'))
                val.erase(val.begin());
            if (name == "content-length") {
                try {
                    contentLength = std::stoul(val);
                } catch (...) {
                    sendSimple(fd, 400,
                               "{\"error\":\"bad_content_length\"}", true);
                    goto done;
                }
            } else if (name == "connection" &&
                       lower(val).find("close") != std::string::npos) {
                clientClose = true;
            }
        }

        if (contentLength > kMaxBodyBytes) {
            sendSimple(fd, 413,
                       "{\"error\":\"body_too_large\",\"message\":"
                       "\"request body exceeds 16 MiB\"}",
                       true);
            goto done;
        }

        Request req;
        req.method = method;
        req.path = path;
        if (contentLength > 0 && !readN(fd, buf, pos, contentLength, req.body))
            break;

        // 丢弃已消费前缀，支持 keep-alive 流水线。
        if (pos > 0) { buf.erase(0, pos); pos = 0; }

        Response resp;
        try {
            resp = handler(req);
        } catch (const std::exception& ex) {
            resp.status = 500;
            resp.content_type = "application/json";
            resp.body = std::string("{\"error\":\"internal_error\"}");
            std::cerr << "[server] handler exception: " << ex.what() << "\n";
        }

        std::string h = "HTTP/1.1 " + std::to_string(resp.status) + " " +
                        statusText(resp.status) + "\r\n";
        h += "Content-Type: " + resp.content_type + "\r\n";
        h += "Content-Length: " + std::to_string(resp.body.size()) + "\r\n";
        h += std::string("Connection: ") +
             (clientClose ? "close" : "keep-alive") + "\r\n";
        for (const auto& [name, value] : resp.headers)
            h += name + ": " + value + "\r\n";
        h += "\r\n";
        writeAll(fd, h);
        if (!resp.body.empty()) writeAll(fd, resp.body);
    }
done:
    ::close(fd);
}

}  // namespace

Server::Server(std::string host, int port, Handler handler)
    : host_(std::move(host)), port_(port), handler_(std::move(handler)) {}

void Server::stop() {
    stopping_ = true;
    if (listen_fd_ >= 0) {
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
}

void Server::run() {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) throw std::runtime_error("socket() failed");

    int yes = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(uint16_t(port_));
    if (host_ == "0.0.0.0" || host_.empty())
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
    else if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1)
        throw std::runtime_error("invalid bind host: " + host_);

    if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) <
        0)
        throw std::runtime_error("bind() failed on " + host_ + ":" +
                                 std::to_string(port_) + " (" +
                                 std::strerror(errno) + ")");
    if (::listen(listen_fd_, 64) < 0)
        throw std::runtime_error("listen() failed");

    std::cout << "TF 坐标树校验服务监听于 http://" << host_ << ":" << port_
              << "\n";

    while (!stopping_) {
        sockaddr_in cli{};
        socklen_t clilen = sizeof(cli);
        int cfd = ::accept(listen_fd_, reinterpret_cast<sockaddr*>(&cli),
                           &clilen);
        if (cfd < 0) {
            if (stopping_) break;
            if (errno == EINTR) continue;
            std::cerr << "[server] accept: " << std::strerror(errno) << "\n";
            continue;
        }
        // 一线程一连接；线程自行 close。
        std::thread(handleConnection, cfd, std::cref(handler_)).detach();
    }
}

}  // namespace http
