#include <cstdlib>
#include <iostream>
#include <string>

#include "gridfusion/server.hpp"

int main(int argc, char** argv) {
    gridfusion::ServerConfig sc;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::cerr << name << " requires an argument\n";
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--host")
            sc.host = next("--host");
        else if (a == "--port")
            sc.port = std::stoi(next("--port"));
        else if (a == "-h" || a == "--help") {
            std::cout
                << "Usage: gridfusion-server [--host 127.0.0.1] [--port 8080]\n";
            return 0;
        } else {
            std::cerr << "unknown argument: " << a << '\n';
            return 2;
        }
    }
    return gridfusion::runServer(sc);
}
