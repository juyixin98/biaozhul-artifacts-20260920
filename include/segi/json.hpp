// SPDX-License-Identifier: MIT
// 极简 JSON（RFC 8259 子集）解析与输出，无外部依赖。
// 数字一律保留原始十进制文本（raw），由上层用 BigInt 精确解析，
// 从根本上杜绝 double 解析导致的精度损失。
#pragma once

#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace segi {

enum class JType { Null, Bool, Number, String, Array, Object };

struct JVal {
    JType type = JType::Null;
    bool boolean = false;
    std::string raw;                     // Number 的原始十进制文本
    std::string str;                     // String 内容（已转义还原）
    std::vector<JVal> arr;
    std::vector<std::pair<std::string, JVal>> obj; // 保序

    const JVal* find(const std::string& key) const {
        if (type != JType::Object) return nullptr;
        for (const auto& kv : obj) if (kv.first == key) return &kv.second;
        return nullptr;
    }
    bool is(JType t) const { return type == t; }
};

class JsonError : public std::runtime_error {
public:
    size_t pos;
    JsonError(const std::string& m, size_t p) : std::runtime_error(m + " (at byte " + std::to_string(p) + ")"), pos(p) {}
};

JVal jsonParse(const std::string& text);
std::string jsonDump(const JVal& v, bool pretty = true, unsigned indent = 2);

// 构造辅助
JVal jNum(const std::string& rawDecimal);
JVal jStr(std::string s);
JVal jArr();
JVal jObj();
void jPush(JVal& arr, JVal v);
void jSet(JVal& obj, const std::string& key, JVal v);

} // namespace segi
