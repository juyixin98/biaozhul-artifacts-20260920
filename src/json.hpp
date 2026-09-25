// json.hpp — 极简 JSON 解析（无第三方依赖）
//
// 仅实现本服务所需的 JSON 子集：object / array / string / number /
// true / false / null。坐标字段通过 asInt64() 取整数，带小数点或指数
// 的数字会被拒绝（本服务坐标只接受整数）。
#pragma once

#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

namespace rectunion {

struct JsonValue {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool boolean = false;
    std::string text;  // Num：原始数字文本；Str：解码后的字符串
    std::vector<JsonValue> arr;
    std::vector<std::pair<std::string, JsonValue>> obj;

    const JsonValue* find(const std::string& key) const {
        if (type != Obj) return nullptr;
        for (const auto& kv : obj)
            if (kv.first == key) return &kv.second;
        return nullptr;
    }
};

namespace detail {

class JsonParser {
public:
    JsonParser(const std::string& s) : s_(s) {}

    bool parse(JsonValue& out, std::string& err) {
        skipWs();
        if (!parseValue(out)) {
            err = err_;
            return false;
        }
        skipWs();
        if (pos_ != s_.size()) {
            err = "JSON 解析失败: 根值之后存在多余字符";
            return false;
        }
        return true;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;
    std::string err_;

    bool fail(const std::string& msg) {
        if (err_.empty()) err_ = "JSON 解析失败: " + msg;
        return false;
    }

    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }

    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    bool consume(char c) {
        if (peek() == c) { ++pos_; return true; }
        return false;
    }

    bool expect(char c, const char* what) {
        if (!consume(c)) return fail(std::string("缺少 ") + what);
        return true;
    }

    bool parseValue(JsonValue& v) {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject(v);
            case '[': return parseArray(v);
            case '"': {
                v.type = JsonValue::Str;
                return parseString(v.text);
            }
            case 't': return parseLit("true", true, v);
            case 'f': return parseLit("false", false, v);
            case 'n': return parseNull(v);
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber(v);
                return fail("非法值起始字符");
        }
    }

    bool parseLit(const char* lit, bool val, JsonValue& v) {
        size_t n = 0;
        while (lit[n]) ++n;
        if (s_.compare(pos_, n, lit) != 0) return fail("非法字面量");
        pos_ += n;
        v.type = JsonValue::Bool;
        v.boolean = val;
        return true;
    }

    bool parseNull(JsonValue& v) {
        if (s_.compare(pos_, 4, "null") != 0) return fail("非法字面量");
        pos_ += 4;
        v.type = JsonValue::Null;
        return true;
    }

    bool parseObject(JsonValue& v) {
        v.type = JsonValue::Obj;
        ++pos_; // {
        skipWs();
        if (consume('}')) return true;
        while (true) {
            skipWs();
            if (peek() != '"') return fail("对象键必须是字符串");
            std::string key;
            if (!parseString(key)) return false;
            skipWs();
            if (!expect(':', "':'")) return false;
            JsonValue val;
            if (!parseValue(val)) return false;
            v.obj.emplace_back(std::move(key), std::move(val));
            skipWs();
            if (consume(',')) continue;
            if (consume('}')) break;
            return fail("对象中缺少 ',' 或 '}'");
        }
        return true;
    }

    bool parseArray(JsonValue& v) {
        v.type = JsonValue::Arr;
        ++pos_; // [
        skipWs();
        if (consume(']')) return true;
        while (true) {
            JsonValue item;
            if (!parseValue(item)) return false;
            v.arr.push_back(std::move(item));
            skipWs();
            if (consume(',')) continue;
            if (consume(']')) break;
            return fail("数组中缺少 ',' 或 ']'");
        }
        return true;
    }

    bool parseString(std::string& out) {
        ++pos_; // 开引号
        while (true) {
            if (pos_ >= s_.size()) return fail("字符串未闭合");
            char c = s_[pos_++];
            if (c == '"') return true;
            if (c == '\\') {
                if (pos_ >= s_.size()) return fail("非法转义");
                char e = s_[pos_++];
                switch (e) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'u': {
                        unsigned cp = 0;
                        for (int k = 0; k < 4; ++k) {
                            if (pos_ >= s_.size()) return fail("非法 \\u 转义");
                            char h = s_[pos_++];
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') cp |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') cp |= static_cast<unsigned>(h - 'A' + 10);
                            else return fail("非法 \\u 十六进制位");
                        }
                        // 编码为 UTF-8（含高代理项但无后续低代理项时按替换字符处理）
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            if (pos_ + 1 < s_.size() && s_[pos_] == '\\' && s_[pos_ + 1] == 'u') {
                                size_t save = pos_;
                                pos_ += 2;
                                unsigned lo = 0;
                                bool ok = true;
                                for (int k = 0; k < 4; ++k) {
                                    if (pos_ >= s_.size()) { ok = false; break; }
                                    char h = s_[pos_++];
                                    lo <<= 4;
                                    if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
                                    else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
                                    else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
                                    else { ok = false; break; }
                                }
                                if (ok && lo >= 0xDC00 && lo <= 0xDFFF) {
                                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                } else {
                                    pos_ = save;
                                    cp = 0xFFFD;
                                }
                            } else {
                                cp = 0xFFFD;
                            }
                        } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                            cp = 0xFFFD;
                        }
                        appendUtf8(out, cp);
                        break;
                    }
                    default: return fail("非法转义字符");
                }
            } else if (static_cast<unsigned char>(c) < 0x20) {
                return fail("字符串中存在未转义控制字符");
            } else {
                out.push_back(c);
            }
        }
    }

    static void appendUtf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else if (cp < 0x10000) {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }

    bool parseNumber(JsonValue& v) {
        size_t start = pos_;
        if (consume('-')) {
            if (peek() == '\0') return fail("非法数字");
        }
        if (peek() == '0') {
            ++pos_;
        } else if (peek() >= '1' && peek() <= '9') {
            while (peek() >= '0' && peek() <= '9') ++pos_;
        } else {
            return fail("非法数字整数部分");
        }
        // 分数与指数：语法上接受（合法 JSON），但坐标取整时会被拒绝。
        if (peek() == '.') {
            ++pos_;
            if (!(peek() >= '0' && peek() <= '9')) return fail("非法小数部分");
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (!(peek() >= '0' && peek() <= '9')) return fail("非法指数部分");
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        v.type = JsonValue::Num;
        v.text = s_.substr(start, pos_ - start);
        return true;
    }
};

} // namespace detail

inline bool parseJson(const std::string& s, JsonValue& out, std::string& err) {
    detail::JsonParser p(s);
    return p.parse(out, err);
}

// 仅接受整数形式（无 '.'、'e'、'E'）且落在 int64 范围内的 JSON 数字。
inline bool jsonInt64(const JsonValue& v, long long& out) {
    if (v.type != JsonValue::Num) return false;
    const std::string& t = v.text;
    if (t.find('.') != std::string::npos ||
        t.find('e') != std::string::npos ||
        t.find('E') != std::string::npos) {
        return false;
    }
    size_t i = 0;
    bool neg = false;
    if (i < t.size() && t[i] == '-') { neg = true; ++i; }
    if (i >= t.size()) return false;

    // 与 INT64_MIN / INT64_MAX 的绝对值逐位比较，安全无溢出。
    const char* limit = neg ? "9223372036854775808" : "9223372036854775807";
    std::string digits = t.substr(i);
    // JSON 数字首 0 已由解析器排除前导零（单 0 除外）。
    if (digits.size() > std::char_traits<char>::length(limit)) return false;
    if (digits.size() == std::char_traits<char>::length(limit) && digits > limit) return false;

    unsigned long long mag = 0;
    for (char c : digits) mag = mag * 10 + static_cast<unsigned long long>(c - '0');

    if (neg) {
        if (mag > static_cast<unsigned long long>(9223372036854775808ULL)) return false;
        if (mag == static_cast<unsigned long long>(9223372036854775808ULL)) {
            out = static_cast<long long>(-9223372036854775807LL - 1); // INT64_MIN
        } else {
            out = -static_cast<long long>(mag);
        }
        return true;
    }
    out = static_cast<long long>(mag);
    return true;
}

inline std::string jsonEscape(const std::string& s) {
    std::string out;
    out.push_back('"');
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
    return out;
}

} // namespace rectunion
