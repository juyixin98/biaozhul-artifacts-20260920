#include "http_server.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstring>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace pcr {

namespace {
std::atomic<bool> g_stop{false};

void handleSignal(int) { g_stop.store(true); }

std::string toLower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

const char* statusText(int code) {
    switch (code) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 413: return "Payload Too Large";
        case 422: return "Unprocessable Entity";
        case 500: return "Internal Server Error";
        default: return "OK";
    }
}

bool readAll(int fd, std::string& out, size_t n) {
    out.resize(n);
    size_t got = 0;
    while (got < n) {
        ssize_t r = ::recv(fd, &out[got], n - got, 0);
        if (r <= 0) return false;
        got += static_cast<size_t>(r);
    }
    return true;
}

void sendAll(int fd, const std::string& data) {
    size_t sent = 0;
    while (sent < data.size()) {
        ssize_t w = ::send(fd, data.data() + sent, data.size() - sent, MSG_NOSIGNAL);
        if (w <= 0) return;
        sent += static_cast<size_t>(w);
    }
}

void sendResponse(int fd, const HttpResponse& resp) {
    std::string head = "HTTP/1.1 " + std::to_string(resp.status) + " " + statusText(resp.status) + "\r\n";
    head += "Content-Type: " + resp.contentType + "\r\n";
    head += "Content-Length: " + std::to_string(resp.body.size()) + "\r\n";
    for (const auto& kv : resp.headers) head += kv.first + ": " + kv.second + "\r\n";
    head += "Connection: close\r\n\r\n";
    sendAll(fd, head);
    sendAll(fd, resp.body);
}

// Graceful FIN: shut down the write side after the response is sent and let the
// client finish/close. With SO_LINGER set, close() still returns promptly while
// the kernel keeps the socket long enough to deliver the data (no RST).
void gracefulClose(int fd) {
    ::shutdown(fd, SHUT_WR);
    char buf[1024];
    timeval tv{1, 0};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    for (int i = 0; i < 8; ++i) {
        if (::recv(fd, buf, sizeof(buf), 0) <= 0) break;
    }
    linger lg{1, 2};
    ::setsockopt(fd, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg));
    ::close(fd);
}

HttpResponse simpleError(int status, const std::string& code, const std::string& message) {
    HttpResponse r;
    r.status = status;
    std::string b = "{\"error\":{\"code\":\"" + code + "\",\"message\":\"";
    for (char c : message) {
        if (c == '"' || c == '\\') b += '\\';
        b += c;
    }
    b += "\"}}";
    r.body = b;
    return r;
}

void serveConnection(int fd, const Handler& handler) {
    // 1 MiB cap: 2 * 5000 * 3 doubles plus JSON framing fits comfortably.
    constexpr size_t kMaxRequest = 1u << 20;
    timeval tv{10, 0};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    std::string raw;
    raw.reserve(4096);
    char buf[4096];
    size_t headerEnd = std::string::npos;

    while (headerEnd == std::string::npos) {
        ssize_t r = ::recv(fd, buf, sizeof(buf), 0);
        if (r <= 0) { ::close(fd); return; }
        raw.append(buf, static_cast<size_t>(r));
        headerEnd = raw.find("\r\n\r\n");
        if (raw.size() > kMaxRequest) {
            sendResponse(fd, simpleError(413, "payload_too_large", "request exceeds 1 MiB limit"));
            gracefulClose(fd);
            return;
        }
    }

    std::string head = raw.substr(0, headerEnd);
    std::string body = raw.substr(headerEnd + 4);

    HttpRequest req;
    {
        size_t lineEnd = head.find("\r\n");
        std::string requestLine = head.substr(0, lineEnd);
        size_t sp1 = requestLine.find(' ');
        size_t sp2 = sp1 == std::string::npos ? std::string::npos : requestLine.find(' ', sp1 + 1);
        if (sp1 == std::string::npos || sp2 == std::string::npos) {
            sendResponse(fd, simpleError(400, "malformed_request", "bad request line"));
            gracefulClose(fd);
            return;
        }
        req.method = requestLine.substr(0, sp1);
        req.target = requestLine.substr(sp1 + 1, sp2 - sp1 - 1);
    }

    size_t contentLength = 0;
    size_t pos = head.find("\r\n");
    while (pos != std::string::npos) {
        size_t next = head.find("\r\n", pos + 2);
        std::string line = head.substr(pos + 2, next == std::string::npos ? std::string::npos : next - pos - 2);
        size_t colon = line.find(':');
        if (colon != std::string::npos) {
            std::string name = toLower(line.substr(0, colon));
            std::string value = line.substr(colon + 1);
            while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) value.erase(value.begin());
            while (!value.empty() && (value.back() == ' ' || value.back() == '\t')) value.pop_back();
            if (name == "content-length") {
                try {
                    contentLength = static_cast<size_t>(std::stoull(value));
                } catch (...) {
                    sendResponse(fd, simpleError(400, "malformed_request", "bad Content-Length"));
                    gracefulClose(fd);
                    return;
                }
            } else if (name == "x-content-sha256") {
                req.contentSha256 = toLower(value);
            }
        }
        pos = next;
    }

    if (contentLength > kMaxRequest) {
        // Drain whatever the client is still sending before replying, otherwise
        // the reply collides with an in-flight upload and arrives as a RST.
        // Cap the drain so a bogus huge Content-Length cannot tie us up forever.
        constexpr size_t kMaxDrain = 8u << 20;
        size_t remaining = std::min(contentLength - std::min(contentLength, body.size()),
                                    kMaxDrain);
        char dbuf[8192];
        while (remaining > 0) {
            ssize_t r = ::recv(fd, dbuf, std::min(remaining, sizeof(dbuf)), 0);
            if (r <= 0) break;
            remaining -= static_cast<size_t>(r);
        }
        if (contentLength - body.size() > kMaxDrain) {
            // Too large to drain; half-close read side so the client gets EPIPE
            // promptly rather than hanging.
            ::shutdown(fd, SHUT_RD);
        }
        sendResponse(fd, simpleError(413, "payload_too_large", "request exceeds 1 MiB limit"));
        gracefulClose(fd);
        return;
    }
    if (body.size() < contentLength) {
        std::string rest;
        if (!readAll(fd, rest, contentLength - body.size())) {
            sendResponse(fd, simpleError(400, "malformed_request", "truncated body"));
            gracefulClose(fd);
            return;
        }
        body += rest;
    }
    req.body = body;

    HttpResponse resp;
    try {
        resp = handler(req);
    } catch (const std::exception& e) {
        resp = simpleError(500, "internal_error", e.what());
    } catch (...) {
        resp = simpleError(500, "internal_error", "unknown error");
    }
    sendResponse(fd, resp);
    gracefulClose(fd);
}
}  // namespace

void serveHttp(const std::string& host, int port, int threads, Handler handler) {
    ::signal(SIGPIPE, SIG_IGN);
    ::signal(SIGINT, handleSignal);
    ::signal(SIGTERM, handleSignal);

    int srv = ::socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) throw std::runtime_error(std::string("socket: ") + std::strerror(errno));
    int yes = 1;
    ::setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1)
        throw std::runtime_error("invalid bind host: " + host);
    if (::bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
        throw std::runtime_error(std::string("bind: ") + std::strerror(errno));
    if (::listen(srv, threads * 4) < 0)
        throw std::runtime_error(std::string("listen: ") + std::strerror(errno));

    std::vector<std::thread> pool;
    std::thread watcher([&] {
        while (!g_stop.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        // Wake every thread blocked in accept().
        ::shutdown(srv, SHUT_RDWR);
    });
    for (int i = 0; i < threads; ++i) {
        pool.emplace_back([&] {
            while (!g_stop.load()) {
                sockaddr_in cli{};
                socklen_t cliLen = sizeof(cli);
                int fd = ::accept(srv, reinterpret_cast<sockaddr*>(&cli), &cliLen);
                if (fd < 0) {
                    if (g_stop.load()) break;
                    continue;
                }
                std::thread(serveConnection, fd, std::cref(handler)).detach();
            }
        });
    }
    for (auto& th : pool) th.join();
    watcher.join();
    ::close(srv);
}

}  // namespace pcr
