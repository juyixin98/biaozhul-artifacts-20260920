//
// main.cpp — entry points for the rectangle-union backend.
//
// Usage:
//   rect_union                        read JSON request from stdin, write JSON to stdout
//   rect_union --file <path>          read JSON request from file
//   rect_union --help
//   rect_union serve [--port 8080] [--host 0.0.0.0]
//                                    minimal HTTP/1.1 JSON service:
//                                      POST /union        Content-Type: application/json
//                                      GET  /healthz      -> 200 OK
//
// The server is dependency-free POSIX (one request per connection). It is a
// convenience entry point, not a production-grade HTTP stack.
//
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <cctype>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "app.hpp"

namespace {

constexpr size_t MAX_BODY_BYTES = 16 * 1024 * 1024;
constexpr int HTTP_RECV_TIMEOUT_SEC = 15;

std::string readAllStdin() {
    std::ostringstream ss;
    ss << std::cin.rdbuf();
    return ss.str();
}

std::string readFile(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return "";
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

void printUsage() {
    std::cerr <<
        "rect_union — axis-aligned rectangle union area & perimeter\n"
        "\n"
        "Usage:\n"
        "  rect_union [--file <path>]       process one JSON request (stdin by default)\n"
        "  rect_union serve [--host H] [--port P]\n"
        "                                   run the HTTP JSON service (default 0.0.0.0:8080)\n"
        "  rect_union --help\n"
        "\n"
        "Request: {\"rectangles\":[{\"x1\":..,\"y1\":..,\"x2\":..,\"y2\":..,\"id\":?},...]}\n";
}

std::string statusText(int code) {
    switch (code) {
        case 200: return "200 OK";
        case 400: return "400 Bad Request";
        case 422: return "422 Unprocessable Entity";
        case 404: return "404 Not Found";
        case 405: return "405 Method Not Allowed";
        case 413: return "413 Payload Too Large";
        case 500: return "500 Internal Server Error";
        default:  return std::to_string(code) + " Unknown";
    }
}

bool recvAll(int fd, std::string& buf, size_t limit) {
    char chunk[8192];
    while (true) {
        ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
        if (n > 0) {
            buf.append(chunk, static_cast<size_t>(n));
            if (buf.size() > limit) return false;
        } else if (n == 0) {
            return true;  // peer closed
        } else {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return true;  // timeout
            return false;
        }
    }
}

std::string toLower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

void sendHttp(int fd, int status, const std::string& body, const std::string& contentType) {
    std::ostringstream out;
    out << "HTTP/1.1 " << statusText(status) << "\r\n"
        << "Content-Type: " << contentType << "\r\n"
        << "Content-Length: " << body.size() << "\r\n"
        << "Connection: close\r\n"
        << "\r\n"
        << body;
    std::string s = out.str();
    size_t sent = 0;
    while (sent < s.size()) {
        ssize_t n = ::send(fd, s.data() + sent, s.size() - sent, 0);
        if (n <= 0) {
            if (errno == EINTR) continue;
            return;
        }
        sent += static_cast<size_t>(n);
    }
}

void handleConnection(int client) {
    timeval tv{HTTP_RECV_TIMEOUT_SEC, 0};
    ::setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    std::string raw;
    if (!recvAll(client, raw, MAX_BODY_BYTES + 1) || raw.size() > MAX_BODY_BYTES) {
        sendHttp(client, 413,
                 R"({"ok":false,"error":{"code":"PAYLOAD_TOO_LARGE","message":"request body exceeds 16 MiB"}})",
                 "application/json");
        return;
    }
    if (raw.empty()) return;

    size_t headerEnd = raw.find("\r\n\r\n");
    if (headerEnd == std::string::npos) {
        sendHttp(client, 400,
                 R"({"ok":false,"error":{"code":"BAD_HTTP","message":"malformed HTTP request: no header terminator"}})",
                 "application/json");
        return;
    }
    std::string head = raw.substr(0, headerEnd);
    std::string body = raw.substr(headerEnd + 4);

    std::istringstream lines(head);
    std::string requestLine;
    std::getline(lines, requestLine);
    if (!requestLine.empty() && requestLine.back() == '\r') requestLine.pop_back();

    std::string method, path;
    {
        std::istringstream rl(requestLine);
        std::string version;
        rl >> method >> path >> version;
    }

    long long contentLength = -1;
    std::string line;
    while (std::getline(lines, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string name = toLower(line.substr(0, colon));
        std::string val = line.substr(colon + 1);
        size_t a = val.find_first_not_of(" \t");
        size_t b = val.find_last_not_of(" \t");
        if (a != std::string::npos) val = val.substr(a, b - a + 1);
        if (name == "content-length") {
            try {
                contentLength = std::stoll(val);
            } catch (...) {
                contentLength = -1;
            }
        }
    }

    if (method == "GET" && (path == "/healthz" || path == "/health")) {
        sendHttp(client, 200, R"({"ok":true,"status":"healthy"})", "application/json");
        return;
    }
    // Unknown path takes precedence over method: GET /nope is 404, not 405.
    if (path != "/union") {
        sendHttp(client, 404,
                 R"({"ok":false,"error":{"code":"NOT_FOUND","message":"unknown path; use POST /union"}})",
                 "application/json");
        return;
    }
    if (method != "POST") {
        sendHttp(client, 405,
                 R"({"ok":false,"error":{"code":"METHOD_NOT_ALLOWED","message":"POST /union is the only computation endpoint"}})",
                 "application/json");
        return;
    }
    if (contentLength < 0) {
        sendHttp(client, 400,
                 R"({"ok":false,"error":{"code":"LENGTH_REQUIRED","message":"Content-Length header is required"}})",
                 "application/json");
        return;
    }
    if (static_cast<unsigned long long>(contentLength) > MAX_BODY_BYTES) {
        sendHttp(client, 413,
                 R"({"ok":false,"error":{"code":"PAYLOAD_TOO_LARGE","message":"request body exceeds 16 MiB"}})",
                 "application/json");
        return;
    }
    if (body.size() < static_cast<size_t>(contentLength)) {
        sendHttp(client, 400,
                 R"({"ok":false,"error":{"code":"BAD_REQUEST","message":"truncated request body"}})",
                 "application/json");
        return;
    }
    body.resize(static_cast<size_t>(contentLength));

    ru::app::Response resp = ru::app::processRequest(body);
    sendHttp(client, resp.status, resp.body, "application/json");
}

int runServer(const std::string& host, int port) {
    ::signal(SIGPIPE, SIG_IGN);

    int srv = ::socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        std::cerr << "socket() failed: " << std::strerror(errno) << "\n";
        return 1;
    }
    int yes = 1;
    ::setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        std::cerr << "invalid host address: " << host << "\n";
        ::close(srv);
        return 1;
    }
    if (::bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::cerr << "bind(" << host << ":" << port << ") failed: " << std::strerror(errno) << "\n";
        ::close(srv);
        return 1;
    }
    if (::listen(srv, 32) != 0) {
        std::cerr << "listen() failed: " << std::strerror(errno) << "\n";
        ::close(srv);
        return 1;
    }
    std::cout << "rect_union listening on http://" << host << ":" << port
              << " (POST /union, GET /healthz)\n" << std::flush;

    while (true) {
        sockaddr_in cli{};
        socklen_t clen = sizeof(cli);
        int fd = ::accept(srv, reinterpret_cast<sockaddr*>(&cli), &clen);
        if (fd < 0) {
            if (errno == EINTR) continue;
            std::cerr << "accept() failed: " << std::strerror(errno) << "\n";
            continue;
        }
        handleConnection(fd);
        ::close(fd);
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::string file;
    std::string host = "0.0.0.0";
    int port = 8080;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--help" || a == "-h") {
            printUsage();
            return 0;
        }
        if (a == "--file" || a == "-f") {
            if (i + 1 >= argc) {
                std::cerr << "missing value for " << a << "\n";
                return 2;
            }
            file = argv[++i];
        } else if (a == "serve") {
            for (int j = i + 1; j < argc; ++j) {
                std::string s = argv[j];
                if (s == "--host" && j + 1 < argc) host = argv[++j];
                else if (s == "--port" && j + 1 < argc) port = std::atoi(argv[++j]);
                else {
                    std::cerr << "unknown serve option: " << s << "\n";
                    return 2;
                }
            }
            return runServer(host, port);
        } else {
            std::cerr << "unknown argument: " << a << "\n";
            printUsage();
            return 2;
        }
    }

    std::string payload = file.empty() ? readAllStdin() : readFile(file);
    if (!file.empty() && payload.empty()) {
        std::cerr << "cannot read file: " << file << "\n";
        return 2;
    }
    ru::app::Response resp = ru::app::processRequest(payload);
    std::cout << resp.body << "\n";
    return resp.ok() ? 0 : 1;
}
