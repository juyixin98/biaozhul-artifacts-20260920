// Minimal but real HTTP/1.1 server on POSIX sockets: no third-party HTTP
// stack. One worker thread per connection, bounded body size and receive
// timeout. Supports keep-alive for multiple requests per connection.
#pragma once

#include <atomic>
#include <string>

namespace pcrs {

class HttpServer {
public:
    HttpServer(std::string host, int port);

    // Binds and listens. Returns false on failure; errmsg populated.
    bool start(std::string& errmsg);
    // Accept loop; returns when stop() is called.
    void serve();
    void stop();

private:
    void handleConnection(int fd);

    std::string host_;
    int port_;
    int listen_fd_ = -1;
    std::atomic<bool> stopping_{false};
};

}  // namespace pcrs
