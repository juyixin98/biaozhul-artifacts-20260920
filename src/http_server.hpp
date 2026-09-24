// http_server.hpp —— 基于 POSIX socket 的极简 HTTP/1.1（线程一连接）
#pragma once

#include <atomic>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace http {

struct Request {
    std::string method;
    std::string path;
    std::string body;
};

struct Response {
    int status = 200;
    std::string content_type = "application/json";
    std::string body;
    // 额外响应头，如 {"X-Content-SHA256", "..."}
    std::vector<std::pair<std::string, std::string>> headers;
};

using Handler = std::function<Response(const Request&)>;

class Server {
public:
    Server(std::string host, int port, Handler handler);
    // 阻塞运行直到 stop()（信号处理中调用）。
    void run();
    void stop();

private:
    std::string host_;
    int port_;
    Handler handler_;
    int listen_fd_ = -1;
    std::atomic<bool> stopping_{false};
};

const char* statusText(int code);

}  // namespace http
