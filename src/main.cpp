// main.cpp — point-cloud registration HTTP service entry point.
#include "api.h"
#include "http_server.h"
#include "sha256.h"

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <string>

using namespace pcr;

int main(int argc, char** argv) {
    std::string host = "0.0.0.0";
    int port = 8080;
    int threads = 4;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "missing value for %s\n", name);
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--host") host = next("--host");
        else if (a == "--port") port = std::stoi(next("--port"));
        else if (a == "--threads") threads = std::stoi(next("--threads"));
        else if (a == "--help" || a == "-h") {
            std::printf("Usage: pcr-server [--host 0.0.0.0] [--port 8080] [--threads 4]\n");
            return 0;
        } else {
            std::fprintf(stderr, "unknown argument: %s\n", a.c_str());
            return 2;
        }
    }

    // Prove the cryptographic primitive is live before accepting traffic.
    // SHA-256("abc") = ba7816bf... (FIPS 180-4 known-answer test).
    const std::string kExpect = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
    if (Sha256::hex(Sha256::hash("abc")) != kExpect) {
        std::fprintf(stderr, "FATAL: SHA-256 self-test failed\n");
        return 1;
    }

    std::printf("point-cloud-registration listening on http://%s:%d (threads=%d)\n",
                host.c_str(), port, threads);
    std::fflush(stdout);

    serveHttp(host, port, threads, [](const HttpRequest& req) -> HttpResponse {
        std::string path = req.target;
        if (auto q = path.find('?'); q != std::string::npos) path = path.substr(0, q);
        if (path == "/healthz" || path == "/") return handleHealth(req);
        if (path == "/v1/register") return handleRegister(req);
        if (path == "/v1/verify-hash") return handleVerifyHash(req);
        HttpResponse r;
        r.status = 404;
        r.body = "{\"error\":{\"code\":\"not_found\",\"message\":\"use POST /v1/register\"}}";
        return r;
    });
    std::printf("shutting down\n");
    return 0;
}
