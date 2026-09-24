// pgrid — CLI entry point.
//
//   pgrid gen-secret
//       Print 32 random bytes as a base64url secret for request signing.
//
//   pgrid serve [--host 127.0.0.1] [--port 8080]
//               [--secret-file PATH | --secret-from-env NAME]
//       Run the HTTP fusion service. With neither secret option a fresh
//       secret is generated and printed once on stderr (ephemeral/dev use).
#include "crypto.hpp"
#include "http_server.hpp"
#include "service.hpp"

#include <cstdio>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

namespace {

void usage() {
    std::cerr <<
        "usage:\n"
        "  pgrid gen-secret\n"
        "  pgrid serve [--host H] [--port P] [--secret-file PATH]\n"
        "              [--secret-from-env ENV_NAME]\n";
}

std::string trim(std::string s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    size_t b = s.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    return s.substr(a, b - a + 1);
}

int cmdGenSecret() {
    std::cout << pf::crypto::b64url_encode(
                     pf::crypto::secure_random_bytes(32))
              << '\n';
    return 0;
}

int cmdServe(int argc, char** argv) {
    std::string host = "127.0.0.1";
    int port = 8080;
    std::string secret_file;
    std::string secret_env;

    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::cerr << "missing value for " << name << '\n';
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--host") host = next("--host");
        else if (a == "--port") port = std::stoi(next("--port"));
        else if (a == "--secret-file") secret_file = next("--secret-file");
        else if (a == "--secret-from-env")
            secret_env = next("--secret-from-env");
        else {
            std::cerr << "unknown argument: " << a << '\n';
            usage();
            return 2;
        }
    }

    std::string secret;
    if (!secret_file.empty()) {
        std::ifstream f(secret_file);
        if (!f) {
            std::cerr << "cannot open secret file: " << secret_file << '\n';
            return 1;
        }
        std::ostringstream ss;
        ss << f.rdbuf();
        secret = trim(ss.str());
    } else if (!secret_env.empty()) {
        const char* v = std::getenv(secret_env.c_str());
        if (!v) {
            std::cerr << "env var not set: " << secret_env << '\n';
            return 1;
        }
        secret = trim(v);
    }
    if (secret.empty()) {
        secret = pf::crypto::b64url_encode(
            pf::crypto::secure_random_bytes(32));
        std::cerr << "[pgrid] WARNING no secret supplied; generated an "
                     "ephemeral secret (clients need it to sign requests):\n"
                  << secret << '\n';
    }
    if (secret.size() < 16) {
        std::cerr << "secret too short (need >= 16 chars)\n";
        return 1;
    }

    pf::MapService service;
    pf::ServerOptions opt;
    opt.host = host;
    opt.port = port;
    opt.secret = secret;
    pf::FusionHttpServer server(service, opt);

    std::cerr << "[pgrid] grid probability fusion listening on http://"
              << host << ':' << port << '\n';
    // Ignore SIGPIPE from client disconnects; httplib normally handles this,
    // but set the process-wide disposition explicitly for safety.
    std::signal(SIGPIPE, SIG_IGN);
    int bound = server.run();
    if (bound == 0) {
        std::cerr << "[pgrid] failed to bind to " << host << ':' << port
                  << '\n';
        return 1;
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        usage();
        return 2;
    }
    std::string cmd = argv[1];
    if (cmd == "gen-secret") return cmdGenSecret();
    if (cmd == "serve") return cmdServe(argc, argv);
    if (cmd == "-h" || cmd == "--help" || cmd == "help") {
        usage();
        return 0;
    }
    std::cerr << "unknown command: " << cmd << '\n';
    usage();
    return 2;
}
