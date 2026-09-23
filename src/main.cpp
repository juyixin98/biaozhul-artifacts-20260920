// gridtopo_check —— 三角网格拓扑校验离线后端
//
// 用法:
//   gridtopo_check [request.json] [-o report.json]
//   cat request.json | gridtopo_check
//
// 退出码:
//   0  请求合法，网格通过全部检查（无几何退化、无拓扑错误；开曲面可正常通过）
//   1  请求合法，但发现几何退化或拓扑错误
//   2  请求本身无法处理（JSON 语法错误、字段缺失、越界引用等）

#include "json.hpp"
#include "mesh.hpp"
#include "topology.hpp"

#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

int main(int argc, char** argv) {
    std::string inputPath;
    std::string outputPath;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "-o" || arg == "--output") {
            if (i + 1 >= argc) {
                std::cerr << "error: " << arg << " requires a file path\n";
                return 2;
            }
            outputPath = argv[++i];
        } else if (arg == "-h" || arg == "--help") {
            std::cout <<
                "usage: gridtopo_check [request.json] [-o report.json]\n"
                "       cat request.json | gridtopo_check\n"
                "exit codes: 0 valid, 1 errors found, 2 bad request\n";
            return 0;
        } else if (!arg.empty() && arg[0] == '-') {
            std::cerr << "error: unknown option '" << arg << "'\n";
            return 2;
        } else {
            inputPath = arg;
        }
    }

    std::string text;
    if (inputPath.empty() || inputPath == "-") {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        text = ss.str();
    } else {
        std::ifstream in(inputPath);
        if (!in) {
            std::cerr << "error: cannot open input file '" << inputPath << "'\n";
            return 2;
        }
        std::ostringstream ss;
        ss << in.rdbuf();
        text = ss.str();
    }

    json::Value root;
    try {
        root = json::parse(text);
    } catch (const json::ParseError& e) {
        json::Value err = json::Value::object();
        err.set("status", json::Value("request_error"));
        err.set("phase", json::Value("json_parse"));
        err.set("message", json::Value(e.message));
        err.set("line", json::Value(e.line));
        err.set("column", json::Value(e.column));
        std::cout << json::dump(err);
        return 2;
    }

    gridtopo::Mesh mesh;
    try {
        mesh = gridtopo::loadMesh(root);
    } catch (const std::exception& e) {
        json::Value err = json::Value::object();
        err.set("status", json::Value("request_error"));
        err.set("phase", json::Value("mesh_load"));
        err.set("message", json::Value(e.what()));
        std::cout << json::dump(err);
        return 2;
    }

    gridtopo::ValidationReport report = gridtopo::validate(mesh);
    std::string out = json::dump(gridtopo::reportToJson(mesh, report));
    std::cout << out;

    if (!outputPath.empty()) {
        std::ofstream of(outputPath);
        if (!of) {
            std::cerr << "error: cannot write output file '" << outputPath << "'\n";
            return 2;
        }
        of << out;
    }

    return report.valid ? 0 : 1;
}
