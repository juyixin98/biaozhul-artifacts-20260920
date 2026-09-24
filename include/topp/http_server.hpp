#pragma once
#include <atomic>

namespace topp {

// Minimal blocking HTTP/1.1 server on POSIX sockets. One detached thread per
// connection; enough for a local backend service without third-party deps.
class HttpServer {
public:
    HttpServer(const char* host, int port);
    ~HttpServer();

    void start();      // binds and listens (returns once listening)
    void stop();       // signals shutdown and closes the listening socket
    void serve();      // accept loop; returns after stop()
    int port() const { return port_; }

private:
    const char* host_;
    int port_;
    int listenFd_;
    std::atomic<bool> running_{false};
};

} // namespace topp
