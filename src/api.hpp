// api.hpp —— HTTP JSON API 路由
#pragma once

#include "http_server.hpp"
#include "transform_tree.hpp"

namespace api {

class App {
public:
    explicit App(tftree::TransformTree& tree) : tree_(tree) {}
    http::Response handle(const http::Request& req);

private:
    tftree::TransformTree& tree_;
};

}  // namespace api
