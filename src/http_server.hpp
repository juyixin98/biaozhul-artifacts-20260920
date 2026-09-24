// HTTP frontend: authenticated REST API over cpp-httplib.
//
// Every stateful route requires HMAC-SHA256 request authentication:
//   X-PF-Timestamp: unix seconds (must be within +-300s of server time)
//   X-PF-Nonce:     unique opaque string (single use, remembered 10 min)
//   X-PF-Signature: hex HMAC_SHA256(secret, canonicalRequest(...))
// Missing/bad credentials => 401; stale timestamp / reused nonce => 401.
#pragma once

#include "service.hpp"

#include <httplib.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_set>

namespace pf {

struct ServerOptions {
    std::string host = "127.0.0.1";
    int port = 8080;
    std::string secret;
    std::int64_t timestamp_tolerance_seconds = 300;
    std::size_t max_body_bytes = 16 * 1024 * 1024;
};

class FusionHttpServer {
public:
    FusionHttpServer(MapService& svc, ServerOptions opt);

    // Binds and serves; returns the bound port on success, 0 on failure.
    // Use stop() from another thread to shut down.
    int run();
    void stop();

    int boundPort() const { return bound_port_.load(); }

private:
    bool authenticate(const httplib::Request& req, std::string& error_code);

    MapService& svc_;
    ServerOptions opt_;
    std::atomic<int> bound_port_{0};
    std::atomic<bool> stopping_{false};
    std::thread thread_;
    std::mutex http_mu_;
    std::shared_ptr<httplib::Server> http_;  // set while run() executes

    std::mutex nonce_mu_;
    std::unordered_set<std::string> seen_nonces_;  // pruned with timestamps

    friend class ServerRunner;
};

// Convenience for tests: own a server thread and wait until /health answers.
class ServerRunner {
public:
    ServerRunner(MapService& svc, ServerOptions opt);
    ~ServerRunner();
    bool waitReady(int timeout_ms = 3000);
    int port() const { return port_; }
    void stop();

private:
    FusionHttpServer server_;
    std::thread thread_;
    int port_ = 0;
};

}  // namespace pf
