#include "topp/http_server.hpp"
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>

using topp::HttpServer;

static HttpServer* g_server = nullptr;

static void onSignal(int) {
    if (g_server) g_server->stop();
}

int main(int argc, char** argv) {
    const char* host = "127.0.0.1";
    int port = 8080;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if ((arg == "--host" || arg == "-h") && i + 1 < argc)
            host = argv[++i];
        else if ((arg == "--port" || arg == "-p") && i + 1 < argc)
            port = std::atoi(argv[++i]);
        else if (arg == "--help") {
            std::cout
                << "Usage: topp_server [--host 127.0.0.1] [--port 8080]\n"
                   "\n"
                   "Endpoints:\n"
                   "  POST /parameterize  joint path time parameterization\n"
                   "  GET  /healthz       health + checksum self-tag\n";
            return 0;
        } else {
            std::cerr << "unknown argument: " << arg << "\n";
            return 2;
        }
    }

    HttpServer server(host, port);
    g_server = &server;
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    try {
        server.start();
    } catch (const std::exception& e) {
        std::cerr << "failed to start server on " << host << ":" << port
                  << ": " << e.what() << "\n";
        return 1;
    }
    std::cerr << "[topp] listening on http://" << host << ":" << server.port()
              << "  (POST /parameterize, GET /healthz)\n";
    server.serve();
    std::cerr << "[topp] stopped\n";
    return 0;
}
