// main.cpp — 命令行 JSON 请求入口
//
// 用法：
//   sphdist [request.json]            从文件或标准输入读取一个请求
//   echo '...' | sphdist
//
// 退出码：
//   0 成功（200）；1 请求错误（400）；2 内部错误（500）；3 用法/IO 错误
//
// 响应为 JSON 文本，打印到 stdout（末尾换行）。
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#if defined(__unix__) || defined(__APPLE__)
#include <unistd.h>  // isatty
#endif

#include "service.hpp"

namespace {

void printUsage() {
    std::cerr <<
        "usage: sphdist [request.json]\n"
        "       cat request.json | sphdist\n"
        "  Reads one JSON request (action: distance | range) from a file\n"
        "  or stdin and writes the JSON response to stdout.\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::string text;

    if (argc > 2) {
        printUsage();
        return 3;
    }
    if (argc == 2) {
        if (std::strcmp(argv[1], "-h") == 0 || std::strcmp(argv[1], "--help") == 0) {
            printUsage();
            return 0;
        }
        std::ifstream f(argv[1], std::ios::binary);
        if (!f) {
            std::cerr << "error: cannot open '" << argv[1] << "'\n";
            return 3;
        }
        std::ostringstream ss;
        ss << f.rdbuf();
        text = ss.str();
    } else {
        // 无参数：从 stdin 读取（管道/重定向场景）
#if defined(__unix__) || defined(__APPLE__)
        if (isatty(0)) {  // 交互式终端且无输入：提示而非挂起
            printUsage();
            return 3;
        }
#endif
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        text = ss.str();
    }

    svc::Response resp = svc::handleRequest(text);
    std::cout << resp.body << '\n';

    if (resp.status == 200) return 0;
    if (resp.status >= 400 && resp.status < 500) return 1;
    return 2;
}
