// 三维射线-包围盒求交后端入口。
//
// 用法：
//   ray_box_backend < 请求.json
//   ray_box_backend --file 请求.json
//   echo '{...}' | ray_box_backend
//
// 输入：单个 JSON 请求（见 samples/ 与 README）。
// 输出：单行紧凑 JSON 到 stdout；人类可读错误写 stderr。
// 退出码：0 正常（含命中/未命中/一致性结果）；2 请求错误（JSON 非法/参数非法）。
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "app/query.h"

int main(int argc, char** argv) {
    std::string requestText;

    if (argc == 1) {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        requestText = ss.str();
    } else if (argc == 3 && std::string(argv[1]) == "--file") {
        std::ifstream in(argv[2]);
        if (!in) {
            std::cerr << "无法打开输入文件：" << argv[2] << "\n";
            return 2;
        }
        std::ostringstream ss;
        ss << in.rdbuf();
        requestText = ss.str();
    } else {
        std::cerr << "用法：\n"
                  << "  ray_box_backend < request.json\n"
                  << "  ray_box_backend --file request.json\n";
        return 2;
    }

    const std::string response = app::handleRequest(requestText);
    std::cout << response << "\n";

    // ok:false 仅可能是请求级错误；几何结果的“未命中”仍为 ok:true。
    if (response.rfind("{\"ok\":false", 0) == 0) return 2;
    return 0;
}
