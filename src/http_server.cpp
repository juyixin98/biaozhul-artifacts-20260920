#include "topp/http_server.hpp"
#include "topp/api.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <chrono>
#include <cstring>
#include <iostream>
#include <sstream>
#include <thread>

namespace topp {

namespace {

constexpr size_t MAX_BODY = 16 * 1024 * 1024;

void setBlockingTimeout(int fd, int seconds) {
    timeval tv{seconds, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
}

std::string statusText(int code) {
    switch (code) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 413: return "Payload Too Large";
        case 422: return "Unprocessable Entity";
        case 500: return "Internal Server Error";
        default: return "Status";
    }
}

void sendAll(int fd, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        ssize_t n = ::send(fd, data.data() + off, data.size() - off, 0);
        if (n <= 0) {
            if (errno == EINTR) continue;
            return;  // client gone; nothing to do
        }
        off += static_cast<size_t>(n);
    }
}

void sendReply(int fd, const HttpReply& reply) {
    std::ostringstream oss;
    oss << "HTTP/1.1 " << reply.status << ' ' << statusText(reply.status)
        << "\r\n"
        << "Content-Type: " << reply.contentType << "\r\n"
        << "Content-Length: " << reply.body.size() << "\r\n"
        << "Connection: close\r\n"
        << "Cache-Control: no-store\r\n"
        << "X-Content-Type-Options: nosniff\r\n"
        << "\r\n";
    std::string head = oss.str();
    sendAll(fd, head);
    sendAll(fd, reply.body);
}

std::string makeError(int /*status*/, const std::string& code,
                      const std::string& message) {
    JsonValue o = JsonValue::object();
    o.asObject()["error"] = JsonValue(code);
    o.asObject()["message"] = JsonValue(message);
    return o.dump();
}

void handleConnection(int fd) {
    setBlockingTimeout(fd, 30);
    std::string buf;
    char chunk[8192];

    size_t headerEnd = std::string::npos;
    size_t contentLength = 0;
    while (true) {
        ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
        if (n <= 0) { ::close(fd); return; }
        buf.append(chunk, static_cast<size_t>(n));
        headerEnd = buf.find("\r\n\r\n");
        if (headerEnd != std::string::npos) break;
        if (buf.size() > 1u << 20) { ::close(fd); return; }
    }

    std::string headerBlock = buf.substr(0, headerEnd);
    std::istringstream hs(headerBlock);
    std::string requestLine;
    std::getline(hs, requestLine);
    if (!requestLine.empty() && requestLine.back() == '\r')
        requestLine.pop_back();

    std::string method, path, version;
    {
        std::istringstream rl(requestLine);
        rl >> method >> path >> version;
    }

    size_t contentLengthHeader = 0;
    bool haveLength = false;
    std::string line;
    while (std::getline(hs, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string name = line.substr(0, colon);
        std::string val = line.substr(colon + 1);
        while (!val.empty() && (val.front() == ' ' || val.front() == '\t'))
            val.erase(val.begin());
        for (auto& c : name) c = static_cast<char>(::tolower(c));
        if (name == "content-length") {
            try {
                contentLengthHeader = std::stoull(val);
                haveLength = true;
            } catch (...) {
                sendReply(fd, {400, "application/json",
                               makeError(400, "BAD_REQUEST",
                                         "invalid Content-Length")});
                ::close(fd);
                return;
            }
        }
    }
    contentLength = haveLength ? contentLengthHeader : 0;
    if (contentLength > MAX_BODY) {
        sendReply(fd, {413, "application/json",
                       makeError(413, "BAD_REQUEST", "request body too large")});
        ::close(fd);
        return;
    }

    std::string body = buf.substr(headerEnd + 4);
    while (body.size() < contentLength) {
        ssize_t n = ::recv(fd, chunk,
                           std::min(sizeof(chunk), contentLength - body.size()),
                           0);
        if (n <= 0) { ::close(fd); return; }
        body.append(chunk, static_cast<size_t>(n));
    }
    body.resize(contentLength);

    HttpReply reply;
    if (path == "/healthz" || path == "/health") {
        if (method != "GET") {
            reply = {405, "application/json",
                     makeError(405, "BAD_REQUEST", "use GET")};
        } else {
            reply = handleHealth();
        }
    } else if (path == "/parameterize") {
        if (method != "POST") {
            reply = {405, "application/json",
                     makeError(405, "BAD_REQUEST", "use POST")};
        } else {
            reply = handleParameterize(body);
        }
    } else {
        reply = {404, "application/json",
                 makeError(404, "BAD_REQUEST",
                           "not found; endpoints are POST /parameterize and "
                           "GET /healthz")};
    }
    sendReply(fd, reply);
    ::close(fd);
}

} // namespace

HttpServer::HttpServer(const char* host, int port)
    : host_(host), port_(port), listenFd_(-1) {}

HttpServer::~HttpServer() { stop(); }

void HttpServer::start() {
    listenFd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listenFd_ < 0)
        throw std::runtime_error(std::string("socket: ") + std::strerror(errno));

    int yes = 1;
    setsockopt(listenFd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port_));
    if (std::strcmp(host_, "0.0.0.0") == 0)
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
    else if (::inet_pton(AF_INET, host_, &addr.sin_addr) != 1)
        throw std::runtime_error("invalid bind host: " + std::string(host_));

    if (::bind(listenFd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
        throw std::runtime_error(std::string("bind: ") + std::strerror(errno));
    if (::listen(listenFd_, 64) < 0)
        throw std::runtime_error(std::string("listen: ") + std::strerror(errno));

    sockaddr_in actual{};
    socklen_t alen = sizeof(actual);
    if (::getsockname(listenFd_, reinterpret_cast<sockaddr*>(&actual), &alen)
        == 0)
        port_ = ntohs(actual.sin_port);

    running_ = true;
}

void HttpServer::stop() {
    if (!running_.exchange(false)) return;
    if (listenFd_ >= 0) {
        ::shutdown(listenFd_, SHUT_RDWR);
        ::close(listenFd_);
        listenFd_ = -1;
    }
}

void HttpServer::serve() {
    signal(SIGPIPE, SIG_IGN);
    while (running_) {
        sockaddr_in caddr{};
        socklen_t clen = sizeof(caddr);
        int fd = ::accept(listenFd_, reinterpret_cast<sockaddr*>(&caddr),
                          &clen);
        if (fd < 0) {
            if (!running_) break;
            if (errno == EINTR) continue;
            continue;
        }
        int one = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        std::thread([fd] { handleConnection(fd); }).detach();
    }
}

} // namespace topp
