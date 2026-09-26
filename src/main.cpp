// SPDX-License-Identifier: MIT
// Entry point with two transports sharing the same JSON API:
//   ./dagpaths                      stdin/stdout, one JSON request per line
//   ./dagpaths --serve [--port N]   minimal HTTP/1.1 server (POST /)
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <csignal>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "api.h"
#include "json.h"

namespace {

constexpr int DEFAULT_PORT = 8080;
constexpr std::size_t MAX_BODY_BYTES = 16 * 1024 * 1024; // 16 MiB
constexpr int BACKLOG = 32;

std::string handleRawRequest(const std::string& raw) {
    try {
        const dagpaths::JsonValue request = dagpaths::parseJson(raw);
        return dagpaths::handleRequest(request).dump();
    } catch (const std::invalid_argument& e) {
        return dagpaths::errorResponse("INVALID_JSON", e.what()).dump();
    }
}

int runCli() {
    // Reads a stream of JSON documents from stdin: they may be
    // pretty-printed (multi-line), concatenated, or one per line.
    // Each document produces exactly one response line. A malformed
    // document yields one INVALID_JSON response; parsing resynchronizes
    // at the next newline so later documents are still processed.
    std::ostringstream buffer;
    buffer << std::cin.rdbuf();
    const std::string input = buffer.str();

    std::size_t pos = 0;
    while (true) {
        while (pos < input.size()) {
            const char c = input[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos;
            else break;
        }
        if (pos >= input.size()) break;

        const std::size_t docStart = pos;
        try {
            const dagpaths::JsonValue request = dagpaths::parseJsonAt(input, pos);
            std::cout << dagpaths::handleRequest(request).dump() << '\n';
        } catch (const std::exception& e) {
            // Invalid input is a normal response-level outcome, not a
            // process failure; resync at the next newline (NDJSON framing).
            std::cout << dagpaths::errorResponse("INVALID_JSON", e.what()).dump() << '\n';
            const std::size_t nl = input.find('\n', docStart);
            pos = (nl == std::string::npos) ? input.size() : nl + 1;
        }
        if (!std::cout.good()) return 1;
    }
    return 0;
}

bool readExact(int fd, char* buffer, std::size_t n) {
    std::size_t got = 0;
    while (got < n) {
        const ssize_t r = ::read(fd, buffer + got, n - got);
        if (r == 0) return false; // peer closed
        if (r < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        got += static_cast<std::size_t>(r);
    }
    return true;
}

// Returns the HTTP body, or empty with badRequest set on protocol errors.
std::string readHttpRequest(int fd, bool& badRequest, bool& closed) {
    badRequest = false;
    closed = false;
    std::string stream;
    char ch = 0;

    auto headerEnd = [&]() -> std::size_t {
        const std::size_t pos = stream.find("\r\n\r\n");
        return pos == std::string::npos ? std::string::npos : pos + 4;
    };

    while (headerEnd() == std::string::npos) {
        if (!readExact(fd, &ch, 1)) { closed = true; return {}; }
        stream.push_back(ch);
        if (stream.size() > MAX_BODY_BYTES) { badRequest = true; return {}; }
    }
    const std::size_t headerLen = headerEnd();
    const std::string headers = stream.substr(0, headerLen);

    std::istringstream parser(headers);
    std::string method, target, version;
    parser >> method >> target >> version;
    if (method != "POST" || target != "/") { badRequest = true; return {}; }

    std::size_t contentLength = 0;
    bool hasLength = false;
    std::string headerLine;
    std::getline(parser, headerLine); // consume request-line remainder
    while (std::getline(parser, headerLine)) {
        if (headerLine.size() >= 2) headerLine.resize(headerLine.size() - 1); // strip \r
        if (headerLine.empty()) break;
        const std::size_t colon = headerLine.find(':');
        if (colon == std::string::npos) continue;
        std::string name = headerLine.substr(0, colon);
        std::string value = headerLine.substr(colon + 1);
        for (char& c : name) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        const std::size_t first = value.find_first_not_of(" \t");
        value = first == std::string::npos ? "" : value.substr(first);
        if (name == "content-length") {
            try {
                contentLength = std::stoull(value);
                hasLength = true;
            } catch (const std::exception&) {
                badRequest = true;
                return {};
            }
        }
    }
    if (!hasLength) { badRequest = true; return {}; }
    if (contentLength > MAX_BODY_BYTES) { badRequest = true; return {}; }

    std::string body = stream.substr(headerLen);
    while (body.size() < contentLength) {
        char buffer[4096];
        const std::size_t want =
            std::min(sizeof(buffer), contentLength - body.size());
        if (!readExact(fd, buffer, want)) { closed = true; return {}; }
        body.append(buffer, want);
    }
    return body;
}

void writeAll(int fd, const std::string& data) {
    std::size_t sent = 0;
    while (sent < data.size()) {
        const ssize_t w = ::write(fd, data.data() + sent, data.size() - sent);
        if (w < 0) {
            if (errno == EINTR) continue;
            return;
        }
        sent += static_cast<std::size_t>(w);
    }
}

void sendHttp(int fd, int status, const std::string& reason,
              const std::string& body) {
    std::ostringstream out;
    out << "HTTP/1.1 " << status << ' ' << reason << "\r\n"
        << "Content-Type: application/json; charset=utf-8\r\n"
        << "Content-Length: " << body.size() << "\r\n"
        << "Connection: close\r\n"
        << "\r\n" << body;
    writeAll(fd, out.str());
}

int runServer(int port) {
    // SIGPIPE from writes to closed sockets would kill the server.
    std::signal(SIGPIPE, SIG_IGN);

    const int serverFd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (serverFd < 0) {
        std::cerr << "socket() failed: " << std::strerror(errno) << '\n';
        return 1;
    }
    int reuse = 1;
    ::setsockopt(serverFd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::bind(serverFd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        std::cerr << "bind() failed on port " << port << ": "
                  << std::strerror(errno) << '\n';
        ::close(serverFd);
        return 1;
    }
    if (::listen(serverFd, BACKLOG) < 0) {
        std::cerr << "listen() failed: " << std::strerror(errno) << '\n';
        ::close(serverFd);
        return 1;
    }
    std::cerr << "dagpaths listening on http://0.0.0.0:" << port
              << " (POST /, Ctrl-C to stop)\n";

    while (true) {
        sockaddr_in client{};
        socklen_t clientLen = sizeof(client);
        const int clientFd =
            ::accept(serverFd, reinterpret_cast<sockaddr*>(&client), &clientLen);
        if (clientFd < 0) {
            if (errno == EINTR) continue;
            std::cerr << "accept() failed: " << std::strerror(errno) << '\n';
            continue;
        }
        bool badRequest = false, closed = false;
        const std::string body = readHttpRequest(clientFd, badRequest, closed);
        if (badRequest) {
            sendHttp(clientFd, 400, "Bad Request",
                     dagpaths::errorResponse(
                         "BAD_HTTP",
                         "expected POST / with Content-Length and JSON body")
                         .dump());
        } else if (!closed) {
            sendHttp(clientFd, 200, "OK", handleRawRequest(body));
        }
        ::close(clientFd);
    }
}

void printUsage() {
    std::cerr
        << "Usage:\n"
        << "  dagpaths                        Read one JSON request per stdin line\n"
        << "  dagpaths --serve [--port PORT]  Serve HTTP on POST / (default port "
        << DEFAULT_PORT << ")\n";
}

} // namespace

int main(int argc, char** argv) {
    bool serve = false;
    int port = DEFAULT_PORT;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--serve") {
            serve = true;
        } else if (arg == "--port") {
            if (i + 1 >= argc) { printUsage(); return 2; }
            try {
                port = std::stoi(argv[++i]);
            } catch (const std::exception&) {
                printUsage();
                return 2;
            }
            if (port < 1 || port > 65535) { printUsage(); return 2; }
        } else if (arg == "--help" || arg == "-h") {
            printUsage();
            return 0;
        } else {
            printUsage();
            return 2;
        }
    }
    return serve ? runServer(port) : runCli();
}
