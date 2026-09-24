// http_server.h — small thread-per-connection HTTP/1.1 server (POSIX sockets).
#pragma once
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace pcr {

struct HttpRequest {
    std::string method;
    std::string target;  // path (query string, if any, is included)
    std::string body;
    bool hasContentHash() const { return !contentSha256.empty(); }
    std::string contentSha256;  // value of X-Content-SHA256, lowercased
};

struct HttpResponse {
    int status = 200;
    std::string contentType = "application/json";
    std::string body;
    // Extra headers, e.g. "X-Content-SHA256".
    std::vector<std::pair<std::string, std::string>> headers;
};

using Handler = std::function<HttpResponse(const HttpRequest&)>;

// Blocks serving until SIGINT/SIGTERM. host may be "0.0.0.0" or "127.0.0.1".
void serveHttp(const std::string& host, int port, int threads, Handler handler);

}  // namespace pcr
