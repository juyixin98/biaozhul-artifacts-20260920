#include "json/json.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <sstream>

namespace js {

namespace {

// 将 Unicode 码位以 UTF-8 追加到输出。
void appendUtf8(std::string& out, uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
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

class Parser {
public:
    Parser(const std::string& text, std::string& err)
        : s_(text), err_(err) {}

    JValue parse() {
        skipWs();
        JValue v = parseValue();
        if (!ok_) return v;
        skipWs();
        if (pos_ != s_.size()) fail("trailing characters");
        return v;
    }

    bool ok() const { return ok_; }

private:
    const std::string& s_;
    std::string& err_;
    size_t pos_ = 0;
    bool ok_ = true;

    struct ParseError {};

    [[noreturn]] void fail(const std::string& msg) {
        std::ostringstream oss;
        oss << "JSON 解析错误（位置 " << pos_ << "）：" << msg;
        err_ = oss.str();
        ok_ = false;
        throw ParseError{};
    }

    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }
    char get() { return pos_ < s_.size() ? s_[pos_++] : '\0'; }
    void expect(char c, const char* what) {
        if (get() != c) fail(std::string("缺少 ") + what);
    }

    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos_;
            } else {
                break;
            }
        }
    }

    JValue parseValue() {
        skipWs();
        if (pos_ >= s_.size()) fail("意外结束");
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': return JValue::makeString(parseString());
            case 't':
            case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                fail("无法识别的值");
        }
    }

    JValue parseObject() {
        expect('{', "'{'");
        JValue obj = JValue::makeObject();
        skipWs();
        if (peek() == '}') { get(); return obj; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("对象键必须是字符串");
            std::string key = parseString();
            skipWs();
            expect(':', "':'");
            JValue val = parseValue();
            obj.set(std::move(key), std::move(val));
            skipWs();
            char c = get();
            if (c == '}') break;
            if (c != ',') fail("对象成员后应为 ',' 或 '}'");
        }
        return obj;
    }

    JValue parseArray() {
        expect('[', "'['");
        JValue arr = JValue::makeArray();
        skipWs();
        if (peek() == ']') { get(); return arr; }
        while (true) {
            arr.push(parseValue());
            skipWs();
            char c = get();
            if (c == ']') break;
            if (c != ',') fail("数组元素后应为 ',' 或 ']'");
        }
        return arr;
    }

    std::string parseString() {
        expect('"', "'\"'");
        std::string out;
        while (true) {
            if (pos_ >= s_.size()) fail("字符串未闭合");
            char c = get();
            if (c == '"') break;
            if (c == '\\') {
                if (pos_ >= s_.size()) fail("转义未结束");
                char e = get();
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
                        uint32_t hi = parseHex4();
                        uint32_t cp = hi;
                        if (hi >= 0xD800 && hi <= 0xDBFF) {
                            // 高代理项，必须跟 \uXXXX 低代理项
                            if (pos_ + 1 < s_.size() && s_[pos_] == '\\' &&
                                s_[pos_ + 1] == 'u') {
                                pos_ += 2;
                                uint32_t lo = parseHex4();
                                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                                    cp = 0x10000 + ((hi - 0xD800) << 10) +
                                         (lo - 0xDC00);
                                } else {
                                    fail("无效的 UTF-16 低代理项");
                                }
                            } else {
                                fail("高代理项后缺少低代理项");
                            }
                        } else if (hi >= 0xDC00 && hi <= 0xDFFF) {
                            fail("意外的 UTF-16 低代理项");
                        }
                        appendUtf8(out, cp);
                        break;
                    }
                    default: fail("非法转义字符");
                }
            } else {
                if (static_cast<unsigned char>(c) < 0x20) {
                    fail("字符串中出现未转义的控制字符");
                }
                out.push_back(c);
            }
        }
        return out;
    }

    uint32_t parseHex4() {
        if (pos_ + 4 > s_.size()) fail("\\u 后需要 4 位十六进制数");
        uint32_t v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = get();
            v <<= 4;
            if (c >= '0' && c <= '9') v |= static_cast<uint32_t>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<uint32_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<uint32_t>(c - 'A' + 10);
            else fail("\\u 后需要 4 位十六进制数");
        }
        return v;
    }

    JValue parseBool() {
        if (s_.compare(pos_, 4, "true") == 0) {
            pos_ += 4;
            return JValue::makeBool(true);
        }
        if (s_.compare(pos_, 5, "false") == 0) {
            pos_ += 5;
            return JValue::makeBool(false);
        }
        fail("非法字面量");
    }

    JValue parseNull() {
        if (s_.compare(pos_, 4, "null") == 0) {
            pos_ += 4;
            return JValue();
        }
        fail("非法字面量");
    }

    JValue parseNumber() {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        if (peek() == '0') {
            ++pos_;
        } else if (peek() >= '1' && peek() <= '9') {
            while (peek() >= '0' && peek() <= '9') ++pos_;
        } else {
            fail("非法数字");
        }
        if (peek() == '.') {
            ++pos_;
            if (!(peek() >= '0' && peek() <= '9')) fail("小数部分缺少数字");
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (!(peek() >= '0' && peek() <= '9')) fail("指数部分缺少数字");
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        std::string token = s_.substr(start, pos_ - start);
        try {
            size_t consumed = 0;
            double d = std::stod(token, &consumed);
            if (consumed != token.size()) fail("非法数字");
            return JValue::makeNumber(d);
        } catch (...) {
            fail("数字超出 double 范围");
        }
    }
};

}  // namespace

JValue parse(const std::string& text, bool& ok, std::string& err) {
    Parser p(text, err);
    try {
        JValue v = p.parse();
        ok = p.ok();
        return v;
    } catch (...) {
        ok = false;
        return JValue();
    }
}

// ---- 序列化 ----

namespace {

void dumpString(std::string& out, const std::string& s) {
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
}

void dumpNumber(std::string& out, double d) {
    if (std::isnan(d) || std::isinf(d)) {
        // 标准 JSON 不支持 NaN/Infinity；作为防御输出 null。
        out += "null";
        return;
    }
    // 整数形式（值在 int64 范围内且无小数）直接输出整数文本。
    if (d == std::floor(d) && std::fabs(d) < 9.007199254740992e15) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%lld",
                      static_cast<long long>(d));
        out += buf;
        return;
    }
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.17g", d);
    out += buf;
}

}  // namespace

void JValue::dumpInto(std::string& out) const {
    switch (type_) {
        case JType::Null: out += "null"; break;
        case JType::Bool: out += b_ ? "true" : "false"; break;
        case JType::Number: dumpNumber(out, num_); break;
        case JType::Integer: {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%lld",
                          static_cast<long long>(int_));
            out += buf;
            break;
        }
        case JType::String: dumpString(out, str_); break;
        case JType::Array: {
            out.push_back('[');
            bool first = true;
            for (const auto& v : *arr_) {
                if (!first) out.push_back(',');
                first = false;
                v.dumpInto(out);
            }
            out.push_back(']');
            break;
        }
        case JType::Object: {
            out.push_back('{');
            bool first = true;
            for (const auto& kv : *obj_) {
                if (!first) out.push_back(',');
                first = false;
                dumpString(out, kv.first);
                out.push_back(':');
                kv.second.dumpInto(out);
            }
            out.push_back('}');
            break;
        }
    }
}

std::string JValue::dump() const {
    std::string out;
    dumpInto(out);
    return out;
}

}  // namespace js
