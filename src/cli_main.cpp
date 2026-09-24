// Offline command line: fuse scan files without a server and export the
// numeric grid, or cross-check the production traversal against the
// per-beam reference.
//
//   gridfusion replay  --map map.json --scans scans.json [--csv]
//                       [--logodds] [--bbox] [-o out]
//   gridfusion verify  --map map.json --scans scans.json
//   gridfusion version --map map.json

#include <cmath>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "gridfusion/grid_map.hpp"
#include "gridfusion/protocol.hpp"
#include <nlohmann/json.hpp>

namespace {

std::string readFile(const std::string& path) {
    std::ifstream f(path);
    if (!f) throw std::runtime_error("cannot open file: " + path);
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

std::vector<gridfusion::Scan> readScans(const std::string& path,
                                        const gridfusion::MapConfig& cfg) {
    const std::string body = readFile(path);
    nlohmann::json j;
    try {
        j = nlohmann::json::parse(body);
    } catch (const std::exception& e) {
        throw std::runtime_error(std::string("invalid scans JSON: ") +
                                 e.what());
    }
    if (j.is_object() && j.contains("scans")) j = j["scans"];
    if (!j.is_array())
        throw std::runtime_error(
            "scans file must be an array (or {\"scans\": [...]})");
    std::vector<gridfusion::Scan> scans;
    for (const auto& sj : j) {
        gridfusion::proto::ParseResult pr;
        gridfusion::Scan scan = gridfusion::proto::parseScan(sj.dump(), pr);
        if (!pr.ok) throw std::runtime_error(pr.error);
        if (const std::string err =
                gridfusion::proto::validateScanForMap(scan, cfg);
            !err.empty())
            throw std::runtime_error(err);
        scans.push_back(std::move(scan));
    }
    return scans;
}

gridfusion::MapConfig readMap(const std::string& path) {
    gridfusion::proto::ParseResult pr;
    gridfusion::MapConfig cfg =
        gridfusion::proto::parseConfig(readFile(path), pr);
    if (!pr.ok) throw std::runtime_error(pr.error);
    return cfg;
}

int usage() {
    std::cerr << "usage:\n"
                 "  gridfusion replay  --map map.json --scans scans.json "
                 "[--csv] [--logodds] [--bbox] [-o out]\n"
                 "  gridfusion verify  --map map.json --scans scans.json\n"
                 "  gridfusion version --map map.json\n";
    return 2;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) return usage();
    const std::string cmd = argv[1];

    std::string map_path, scans_path, out_path;
    bool csv = false, logodds = false, bbox = false;
    for (int i = 2; i < argc; ++i) {
        const std::string a = argv[i];
        auto need = [&](const char* n) -> std::string {
            if (i + 1 >= argc)
                throw std::runtime_error(std::string(n) + " needs a value");
            return argv[++i];
        };
        if (a == "--map") map_path = need("--map");
        else if (a == "--scans") scans_path = need("--scans");
        else if (a == "-o") out_path = need("-o");
        else if (a == "--csv") csv = true;
        else if (a == "--logodds") logodds = true;
        else if (a == "--bbox") bbox = true;
        else throw std::runtime_error("unknown argument: " + a);
    }
    if (map_path.empty()) return usage();

    try {
        gridfusion::MapConfig cfg = readMap(map_path);

        if (cmd == "version") {
            std::cout << gridfusion::GridMap::make_version_id(cfg) << '\n';
            return 0;
        }
        if (scans_path.empty()) return usage();
        auto scans = readScans(scans_path, cfg);

        if (cmd == "verify") {
            auto d = gridfusion::GridMap::verifyAgainstReference(cfg, scans);
            std::cout << "cells=" << d.cell_count
                      << " differing=" << d.differing_cells
                      << " max_abs_logit_diff=" << d.max_abs_logit_diff
                      << '\n';
            if (d.differing_cells != 0) {
                std::cerr << "reference traversal mismatch\n";
                return 1;
            }
            std::cout << "OK: production traversal matches per-beam "
                         "reference exactly\n";
            return 0;
        }

        if (cmd == "replay") {
            gridfusion::GridMap map(cfg);
            for (const auto& s : scans) map.update(s);
            gridfusion::proto::ExportOptions opt;
            opt.logodds = logodds;
            opt.bbox_only = bbox;
            const std::string out =
                csv ? gridfusion::proto::gridToCsv(map, opt)
                    : gridfusion::proto::gridToJson(map, opt);
            if (out_path.empty() || out_path == "-")
                std::cout << out;
            else {
                std::ofstream f(out_path);
                f << out;
            }
            return 0;
        }
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << '\n';
        return 1;
    }
    return usage();
}
