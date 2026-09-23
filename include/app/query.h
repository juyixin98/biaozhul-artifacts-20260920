// 请求解析、参数校验与响应组装：连接 JSON 层与几何内核。
#pragma once

#include <string>

#include "json/json.h"

namespace app {

// 处理一个完整 JSON 请求文本，返回完整 JSON 响应文本。
// 任何校验/解析错误都以 {"ok":false,"error":...} 返回（不抛异常）。
std::string handleRequest(const std::string& requestText);

// 仅为响应构造一个错误对象。
std::string errorResponse(const std::string& message);

}  // namespace app
