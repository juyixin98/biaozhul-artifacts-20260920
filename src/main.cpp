// main.cpp —— TF 坐标树校验服务入口
#include "api.hpp"
#include "http_server.hpp"
#include "sha256.hpp"
#include "transform_tree.hpp"

#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>

namespace {
http::Server* g_server = nullptr;

void onSignal(int) {
    if (g_server) g_server->stop();
}
}  // namespace

int main(int argc, char** argv) {
    std::string host = "127.0.0.1";
    int port = 8080;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if ((a == "--host" || a == "-h") && i + 1 < argc)
            host = argv[++i];
        else if ((a == "--port" || a == "-p") && i + 1 < argc)
            port = std::atoi(argv[++i]);
        else {
            std::cerr << "用法: tf_server [--host 127.0.0.1] [--port 8080]\n";
            return 2;
        }
    }

    tftree::TransformTree tree;
    api::App app(tree);

    // 统一出口：每个响应都带上对 body 真实计算的 SHA-256（密码学操作），
    // 客户端可独立复算核对，确认未被篡改/截断。
    http::Handler handler = [&app](const http::Request& req) {
        http::Response resp = app.handle(req);
        resp.headers.push_back(
            {"X-Content-SHA256", sha256::hex(resp.body)});
        return resp;
    };

    http::Server server(host, port, handler);
    g_server = &server;
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    try {
        server.run();
    } catch (const std::exception& e) {
        std::cerr << "服务启动失败: " << e.what() << "\n";
        return 1;
    }
    std::cout << "服务已停止\n";
    return 0;
}
