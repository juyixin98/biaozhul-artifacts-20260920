// json.hpp — 极简、仅依赖标准库的 JSON 解析/序列化器
// 设计目标：满足空间索引后端的请求解析与结果输出需求，不引入第三方依赖。
// - 数字统一保存为 long double；符合整数语法且落在 int64 范围内的数字额外记录整数表示（用于 id / k）。
// - 对象使用 std::map 保存，键顺序确定（字典序），输出稳定，便于测试对照。
#pragma once

#include <cstdint>
#include <cerrno>
#include <climits>
#include <cmath>
#include <iomanip>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace sjson {

class Json {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    Type type = Type::Null;
    bool boolValue = false;

    long double numValue = 0.0L;
    bool isInteger = false;
    std::int64_t intValue = 0;

    std::string strValue;
    std::vector<Json> arr;
    std::map<std::string, Json> obj;

    // ---- 构造辅助 ----
    static Json null() { Json j; j.type = Type::Null; return j; }
    static Json boolean(bool b) { Json j; j.type = Type::Bool; j.boolValue = b; return j; }
    static Json number(long double v) {
        Json j; j.type = Type::Number; j.numValue = v; return j;
    }
    static Json integer(std::int64_t v) {
        Json j; j.type = Type::Number; j.numValue = static_cast<long double>(v);
        j.isInteger = true; j.intValue = v; return j;
    }
    static Json string(std::string v) {
        Json j; j.type = Type::String; j.strValue = std::move(v); return j;
    }
    static Json array() { Json j; j.type = Type::Array; return j; }
    static Json object() { Json j; j.type = Type::Object; return j; }

    bool isObject() const { return type == Type::Object; }
    bool isArray() const { return type == Type::Array; }
    bool isNumber() const { return type == Type::Number; }
    bool isString() const { return type == Type::String; }
    bool isBool() const { return type == Type::Bool; }

    const Json* find(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }

    // ---- 解析 ----
    // 成功返回 true；失败返回 false，err 写入带位置的错误信息。
    static bool parse(const std::string& text, Json& out, std::string& err) {
        Parser p(text);
        p.skipWs();
        if (!p.parseValue(out)) { err = p.error_; return false; }
        p.skipWs();
        if (p.pos_ != p.text_.size()) {
            err = p.fmtError("根值之后存在多余字符");
            return false;
        }
        return true;
    }

    // ---- 序列化 ----
    void dump(std::string& out) const {
        switch (type) {
            case Type::Null: out += "null"; break;
            case Type::Bool: out += boolValue ? "true" : "false"; break;
            case Type::Number: {
                if (isInteger) {
                    out += std::to_string(intValue);
                } else {
                    out += formatFloat(numValue);
                }
                break;
            }
            case Type::String: dumpString(out, strValue); break;
            case Type::Array: {
                out += '[';
                for (size_t i = 0; i < arr.size(); ++i) {
                    if (i) out += ',';
                    arr[i].dump(out);
                }
                out += ']';
                break;
            }
            case Type::Object: {
                out += '{';
                bool first = true;
                for (const auto& kv : obj) {
                    if (!first) out += ',';
                    first = false;
                    dumpString(out, kv.first);
                    out += ':';
                    kv.second.dump(out);
                }
                out += '}';
                break;
            }
        }
    }

    std::string dump() const {
        std::string s;
        dump(s);
        return s;
    }

    std::string dumpPretty(int indent = 0) const {
        std::string s;
        dumpPretty(s, indent);
        return s;
    }

    // 美化输出（2 空格缩进），供人阅读。
    void dumpPretty(std::string& out, int indent = 0) const {
        switch (type) {
            case Type::Null: out += "null"; break;
            case Type::Bool: out += boolValue ? "true" : "false"; break;
            case Type::Number:
                out += isInteger ? std::to_string(intValue) : formatFloat(numValue);
                break;
            case Type::String: dumpString(out, strValue); break;
            case Type::Array: {
                if (arr.empty()) { out += "[]"; break; }
                out += "[\n";
                for (size_t i = 0; i < arr.size(); ++i) {
                    if (i) out += ",\n";
                    appendIndent(out, indent + 1);
                    arr[i].dumpPretty(out, indent + 1);
                }
                out += '\n';
                appendIndent(out, indent);
                out += ']';
                break;
            }
            case Type::Object: {
                if (obj.empty()) { out += "{}"; break; }
                out += "{\n";
                bool first = true;
                for (const auto& kv : obj) {
                    if (!first) out += ",\n";
                    first = false;
                    appendIndent(out, indent + 1);
                    dumpString(out, kv.first);
                    out += ": ";
                    kv.second.dumpPretty(out, indent + 1);
                }
                out += '\n';
                appendIndent(out, indent);
                out += '}';
                break;
            }
        }
    }

    static std::string formatFloat(long double v) {
        // 非有限值不是合法 JSON 数字；调用方应在写入前完成校验。
        if (!std::isfinite(v)) return "null";
        std::ostringstream os;
        // 18 位有效数字：足以无损往返 IEEE-754 double，并保留 x86 80 位 long double 的主要精度。
        os << std::setprecision(18) << v;
        return os.str();
    }

private:
    static void appendIndent(std::string& out, int level) {
        out.append(static_cast<size_t>(level) * 2, ' ');
    }
    static void dumpString(std::string& out, const std::string& s) {
        out += '"';
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
                        out += static_cast<char>(c);  // UTF-8 原样输出
                    }
            }
        }
        out += '"';
    }

    // ---------------- 递归下降解析器 ----------------
    struct Parser {
        const std::string& text_;
        size_t pos_ = 0;
        std::string error_;

        explicit Parser(const std::string& t) : text_(t) {}

        std::string fmtError(const std::string& msg) {
            return "JSON 解析错误(位置 " + std::to_string(pos_) + "): " + msg;
        }
        void fail(const std::string& msg) {
            if (error_.empty()) error_ = fmtError(msg);
        }

        void skipWs() {
            while (pos_ < text_.size()) {
                char c = text_[pos_];
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
                else break;
            }
        }

        bool parseValue(Json& out) {
            skipWs();
            if (pos_ >= text_.size()) { fail("意外结束，缺少值"); return false; }
            char c = text_[pos_];
            switch (c) {
                case '{': return parseObject(out);
                case '[': return parseArray(out);
                case '"': {
                    std::string s;
                    if (!parseString(s)) return false;
                    out = Json::string(std::move(s));
                    return true;
                }
                case 't': return parseLiteral("true", Json::boolean(true), out);
                case 'f': return parseLiteral("false", Json::boolean(false), out);
                case 'n': return parseLiteral("null", Json::null(), out);
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return parseNumber(out);
                    fail(std::string("非法字符 '") + c + "'");
                    return false;
            }
        }

        bool parseLiteral(const char* lit, const Json& val, Json& out) {
            size_t len = std::char_traits<char>::length(lit);
            if (text_.compare(pos_, len, lit) != 0) { fail(std::string("非法字面量，应为 ") + lit); return false; }
            pos_ += len;
            out = val;
            return true;
        }

        bool parseObject(Json& out) {
            ++pos_;  // '{'
            out = Json::object();
            skipWs();
            if (pos_ < text_.size() && text_[pos_] == '}') { ++pos_; return true; }
            while (true) {
                skipWs();
                if (pos_ >= text_.size() || text_[pos_] != '"') { fail("对象键必须是字符串"); return false; }
                std::string key;
                if (!parseString(key)) return false;
                skipWs();
                if (pos_ >= text_.size() || text_[pos_] != ':') { fail("对象键后缺少 ':'"); return false; }
                ++pos_;
                Json val;
                if (!parseValue(val)) return false;
                if (out.obj.count(key)) { fail("对象中存在重复键: " + key); return false; }
                out.obj.emplace(std::move(key), std::move(val));
                skipWs();
                if (pos_ >= text_.size()) { fail("对象未闭合，缺少 '}'"); return false; }
                char c = text_[pos_];
                if (c == ',') { ++pos_; continue; }
                if (c == '}') { ++pos_; return true; }
                fail("对象元素后应为 ',' 或 '}'");
                return false;
            }
        }

        bool parseArray(Json& out) {
            ++pos_;  // '['
            out = Json::array();
            skipWs();
            if (pos_ < text_.size() && text_[pos_] == ']') { ++pos_; return true; }
            while (true) {
                Json val;
                if (!parseValue(val)) return false;
                out.arr.push_back(std::move(val));
                skipWs();
                if (pos_ >= text_.size()) { fail("数组未闭合，缺少 ']'"); return false; }
                char c = text_[pos_];
                if (c == ',') { ++pos_; skipWs(); continue; }
                if (c == ']') { ++pos_; return true; }
                fail("数组元素后应为 ',' 或 ']'");
                return false;
            }
        }

        bool parseString(std::string& out) {
            ++pos_;  // 开引号
            while (pos_ < text_.size()) {
                char c = text_[pos_++];
                if (c == '"') return true;
                if (c == '\\') {
                    if (pos_ >= text_.size()) { fail("转义序列未结束"); return false; }
                    char e = text_[pos_++];
                    switch (e) {
                        case '"': out += '"'; break;
                        case '\\': out += '\\'; break;
                        case '/': out += '/'; break;
                        case 'b': out += '\b'; break;
                        case 'f': out += '\f'; break;
                        case 'n': out += '\n'; break;
                        case 'r': out += '\r'; break;
                        case 't': out += '\t'; break;
                        case 'u': {
                            unsigned code = 0;
                            if (!parseHex4(code)) return false;
                            // 代理对
                            if (code >= 0xD800 && code <= 0xDBFF) {
                                if (pos_ + 2 > text_.size() || text_[pos_] != '\\' || text_[pos_ + 1] != 'u') {
                                    fail("高代理项后缺少 \\u 低代理项"); return false;
                                }
                                pos_ += 2;
                                unsigned lo = 0;
                                if (!parseHex4(lo)) return false;
                                if (lo < 0xDC00 || lo > 0xDFFF) { fail("低代理项非法"); return false; }
                                code = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                            } else if (code >= 0xDC00 && code <= 0xDFFF) {
                                fail("出现孤立的低代理项"); return false;
                            }
                            appendUtf8(out, code);
                            break;
                        }
                        default: fail(std::string("非法转义 '\\") + e + "'"); return false;
                    }
                } else if (static_cast<unsigned char>(c) < 0x20) {
                    fail("字符串中存在未转义的控制字符");
                    return false;
                } else {
                    out += c;
                }
            }
            fail("字符串未闭合");
            return false;
        }

        bool parseHex4(unsigned& code) {
            if (pos_ + 4 > text_.size()) { fail("\\u 后需要 4 位十六进制数"); return false; }
            code = 0;
            for (int i = 0; i < 4; ++i) {
                char h = text_[pos_++];
                code <<= 4;
                if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                else { fail("\\u 后需要 4 位十六进制数"); return false; }
            }
            return true;
        }

        static void appendUtf8(std::string& out, unsigned code) {
            if (code < 0x80) {
                out += static_cast<char>(code);
            } else if (code < 0x800) {
                out += static_cast<char>(0xC0 | (code >> 6));
                out += static_cast<char>(0x80 | (code & 0x3F));
            } else if (code < 0x10000) {
                out += static_cast<char>(0xE0 | (code >> 12));
                out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
                out += static_cast<char>(0x80 | (code & 0x3F));
            } else {
                out += static_cast<char>(0xF0 | (code >> 18));
                out += static_cast<char>(0x80 | ((code >> 12) & 0x3F));
                out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
                out += static_cast<char>(0x80 | (code & 0x3F));
            }
        }

        bool parseNumber(Json& out) {
            size_t start = pos_;
            bool integerSyntax = true;
            if (text_[pos_] == '-') ++pos_;
            if (pos_ >= text_.size()) { fail("数字不完整"); return false; }
            if (text_[pos_] == '0') {
                ++pos_;
            } else if (text_[pos_] >= '1' && text_[pos_] <= '9') {
                while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
            } else { fail("数字整数部分非法"); return false; }
            if (pos_ < text_.size() && text_[pos_] == '.') {
                integerSyntax = false;
                ++pos_;
                if (pos_ >= text_.size() || !(text_[pos_] >= '0' && text_[pos_] <= '9')) {
                    fail("小数部分至少需要一位数字"); return false;
                }
                while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
            }
            if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
                integerSyntax = false;
                ++pos_;
                if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
                if (pos_ >= text_.size() || !(text_[pos_] >= '0' && text_[pos_] <= '9')) {
                    fail("指数部分至少需要一位数字"); return false;
                }
                while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
            }
            std::string token = text_.substr(start, pos_ - start);

            if (integerSyntax) {
                // 尝试按 int64 解析（id / k 使用）
                bool neg = !token.empty() && token[0] == '-';
                size_t i = neg ? 1 : 0;
                // 语法已保证是数字；判断范围
                static const std::string maxAbs = "9223372036854775808"; // abs(INT64_MIN)
                const std::string digits = token.substr(i);
                bool inRange;
                if (digits.size() < maxAbs.size()) inRange = true;
                else if (digits.size() > maxAbs.size()) inRange = false;
                else inRange = digits.compare(maxAbs) <= 0; // 允许 9223372036854775808，仅负数可取
                if (inRange) {
                    // 逐位计算，允许恰好 -9223372036854775808
                    std::uint64_t mag = 0;
                    for (char d : digits) mag = mag * 10 + static_cast<unsigned>(d - '0');
                    if (!neg && mag > static_cast<std::uint64_t>(INT64_MAX)) inRange = false;
                    else if (neg && mag > static_cast<std::uint64_t>(INT64_MAX) + 1) inRange = false;
                    if (inRange) {
                        std::int64_t v = neg ? -static_cast<std::int64_t>(mag)
                                             :  static_cast<std::int64_t>(mag);
                        out = Json::integer(v);
                        return true;
                    }
                }
                // 超出 int64 的整数：退回浮点
            }

            // strtold 对已校验的 JSON 数字语法必能解析。
            char* end = nullptr;
            errno = 0;
            long double v = std::strtold(token.c_str(), &end);
            if (end != token.c_str() + token.size()) { fail("数字无法解析: " + token); return false; }
            if (errno == ERANGE || !std::isfinite(v)) { fail("数字超出 long double 范围: " + token); return false; }
            out = Json::number(v);
            return true;
        }
    };
};

}  // namespace sjson
