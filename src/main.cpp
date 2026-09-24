#include <atomic>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>

#include "server.hpp"

namespace {
std::atomic<pcrs::HttpServer*> g_server{nullptr};

void onSignal(int) {
    pcrs::HttpServer* s = g_server.load();
    if (s) s->stop();
}
}  // namespace

int main(int argc, char** argv) {
    std::string host = "0.0.0.0";
    int port = 8080;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::cerr << "missing value for " << name << "\n";
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--host" || a == "-h") {
            host = next("--host");
        } else if (a == "--port" || a == "-p") {
            port = std::stoi(next("--port"));
        } else if (a == "--help") {
            std::cout
                << "Usage: pcrs_server [--host 0.0.0.0] [--port 8080]\n"
                << "POST /register {\"source\":[[x,y,z]...],\"target\":[...]} "
                   "(max 5000 points each)\n"
                << "GET  /health\n";
            return 0;
        } else {
            std::cerr << "unknown argument: " << a << "\n";
            return 2;
        }
    }

    pcrs::HttpServer server(host, port);
    std::string err;
    if (!server.start(err)) {
        std::cerr << "failed to start server: " << err << "\n";
        return 1;
    }
    g_server = &server;
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    std::cout << "point-cloud-registration service listening on " << host
              << ":" << port << "\n";
    server.serve();
    std::cout << "server stopped\n";
    return 0;
}
