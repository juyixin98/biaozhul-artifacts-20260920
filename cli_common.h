// SPDX-License-Identifier: MIT
// cli_common.h — shared helpers for the command line tools.
#pragma once

#include "shmring.h"

#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace cli {

struct Args {
    std::vector<std::string> pos;
    // long-only options, types: "b" bool flag, "u" uint64, "i" int64, "s" string
    std::vector<std::pair<std::string, std::string>> opt;

    bool has(const std::string& k) const {
        for (auto& kv : opt)
            if (kv.first == k) return true;
        return false;
    }
    std::string get(const std::string& k, const std::string& d = "") const {
        for (auto& kv : opt)
            if (kv.first == k) return kv.second;
        return d;
    }
    uint64_t getu(const std::string& k, uint64_t d) const {
        if (!has(k)) return d;
        return strtoull(get(k).c_str(), nullptr, 10);
    }
    int64_t geti(const std::string& k, int64_t d) const {
        if (!has(k)) return d;
        return strtoll(get(k).c_str(), nullptr, 10);
    }
};

inline void install_stop_handlers() {
    struct sigaction sa;
    std::memset(&sa, 0, sizeof(sa));
    sa.sa_handler = [](int) { shmring::request_stop(); };
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);
    signal(SIGPIPE, SIG_IGN);
}

inline Args parse_args(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; ++i) {
        std::string s = argv[i];
        if (s.rfind("--", 0) == 0) {
            std::string k = s.substr(2);
            std::string v;
            auto eq = k.find('=');
            if (eq != std::string::npos) {
                v = k.substr(eq + 1);
                k = k.substr(0, eq);
                a.opt.emplace_back(k, v);
            } else if (i + 1 < argc && argv[i + 1][0] != '-') {
                a.opt.emplace_back(k, argv[i + 1]);
                ++i;
            } else {
                a.opt.emplace_back(k, "");
            }
        } else {
            a.pos.push_back(s);
        }
    }
    return a;
}

// Append one byte as a JSON-style escape into out (UTF-8 pass-through).
inline void append_escaped(std::string& out, uint8_t b) {
    static const char* hex = "0123456789abcdef";
    switch (b) {
    case '"': out += "\\\""; break;
    case '\\': out += "\\\\"; break;
    case '\n': out += "\\n"; break;
    case '\r': out += "\\r"; break;
    case '\t': out += "\\t"; break;
    default:
        if (b >= 0x20 && b != 0x7f) {
            out.push_back(static_cast<char>(b));
        } else {
            out += "\\u00";
            out.push_back(hex[b >> 4]);
            out.push_back(hex[b & 0xF]);
        }
    }
}

// Deterministic synthetic generator shared by tests:
//   payload = "gen:<seq>:<len>:<fill>" where fill is a repeated byte derived
//   from seq, truncated to len. The consumer test harness reproduces it.
inline std::string gen_payload(uint64_t seq, uint32_t len) {
    uint8_t fill = static_cast<uint8_t>(0x30 + (seq % 26)); // '0'..'z' range
    std::string body(len, static_cast<char>(fill));
    char prefix[64];
    int plen = std::snprintf(prefix, sizeof(prefix), "gen:%llu:%u:",
                             static_cast<unsigned long long>(seq), len);
    std::string out(prefix, plen > 0 ? plen : 0);
    uint32_t room = len > out.size() ? static_cast<uint32_t>(len - out.size())
                                     : 0;
    out.append(body.data(), body.size() > room ? room : body.size());
    out.resize(len, static_cast<char>(fill));
    return out;
}

inline void emit_line(const std::string& s) {
    std::fwrite(s.data(), 1, s.size(), stdout);
    std::fputc('\n', stdout);
}

} // namespace cli
