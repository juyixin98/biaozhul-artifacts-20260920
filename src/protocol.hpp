// HTTP request handling: JSON validation and ICP invocation.
#pragma once

#include <string>

namespace pcrs {

struct HttpResponse {
    int status = 200;
    std::string content_type = "application/json";
    std::string body;
};

// Route a fully received HTTP/1.1 request. method/path/body are already
// extracted by the server. bodyHash is SHA-256 of the raw request body and is
// echoed in successful registration responses.
HttpResponse handleRequest(const std::string& method, const std::string& path,
                           const std::string& body);

}  // namespace pcrs
