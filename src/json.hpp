// json.hpp - 极小的 JSON 读写库（本项目专用，零第三方依赖）
// 支持: null / bool / number(double) / string / array / object
// 数字以 double 存储；输出时整数不带小数点。
#pragma once

#include <cmath>
#include <cstdint>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace mini_json {

struct Value;
using Array = std::vector<Value>;
// 保持插入顺序的 object
using Object = std::vector<std::pair<std::string, Value>>;

enum class Type { Null, Bool, Number, String, Array, Object };

struct Value {
    Type type = Type::Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::shared_ptr<Array> arr;
    std::shared_ptr<Object> obj;

    Value() {}
    Value(bool v) : type(Type::Bool), b(v) {}
    Value(int v) : type(Type::Number), num(static_cast<double>(v)) {}
    Value(long long v) : type(Type::Number), num(static_cast<double>(v)) {}
    Value(double v) : type(Type::Number), num(v) {}
    Value(const char* v) : type(Type::String), str(v) {}
    Value(const std::string& v) : type(Type::String), str(v) {}

    static Value make_array() {
        Value v;
        v.type = Type::Array;
        v.arr = std::make_shared<Array>();
        return v;
    }
    static Value make_object() {
        Value v;
        v.type = Type::Object;
        v.obj = std::make_shared<Object>();
        return v;
    }

    bool is(Type t) const { return type == t; }

    Value& push_back(Value v) {
        if (type != Type::Array) throw std::runtime_error("push_back on non-array");
        arr->push_back(std::move(v));
        return arr->back();
    }

    void set(const std::string& key, Value v) {
        if (type != Type::Object) throw std::runtime_error("set on non-object");
        for (auto& kv : *obj) {
            if (kv.first == key) { kv.second = std::move(v); return; }
        }
        obj->emplace_back(key, std::move(v));
    }
    const Value* find(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        for (const auto& kv : *obj)
            if (kv.first == key) return &kv.second;
        return nullptr;
    }
    bool contains(const std::string& key) const { return find(key) != nullptr; }
};

// ---------------- 输出 ----------------
inline std::string escape_string(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 2);
    out += '"';
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\t': out += "\\t"; break;
            case '\r': out += "\\r"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    out += '"';
    return out;
}

inline std::string format_number(double x) {
    if (std::isnan(x) || std::isinf(x)) return "null";
    // 接近整数则输出整数形式，避免 0.0
    if (x == std::floor(x) && std::abs(x) < 1e15) {
        long long i = static_cast<long long>(x);
        return std::to_string(i);
    }
    std::ostringstream oss;
    oss.precision(17);
    oss << x;
    std::string s = oss.str();
    return s;
}

inline void dump_into(const Value& v, std::string& out, int indent, int level) {
    std::string pad(level * indent, ' ');
    std::string pad_child((level + 1) * indent, ' ');
    switch (v.type) {
        case Type::Null: out += "null"; break;
        case Type::Bool: out += v.b ? "true" : "false"; break;
        case Type::Number: out += format_number(v.num); break;
        case Type::String: out += escape_string(v.str); break;
        case Type::Array: {
            if (v.arr->empty()) { out += "[]"; break; }
            out += "[\n";
            for (size_t i = 0; i < v.arr->size(); ++i) {
                out += pad_child;
                dump_into(v.arr->at(i), out, indent, level + 1);
                if (i + 1 < v.arr->size()) out += ',';
                out += '\n';
            }
            out += pad;
            out += ']';
            break;
        }
        case Type::Object: {
            if (v.obj->empty()) { out += "{}"; break; }
            out += "{\n";
            for (size_t i = 0; i < v.obj->size(); ++i) {
                out += pad_child;
                out += escape_string(v.obj->at(i).first);
                out += ": ";
                dump_into(v.obj->at(i).second, out, indent, level + 1);
                if (i + 1 < v.obj->size()) out += ',';
                out += '\n';
            }
            out += pad;
            out += '}';
            break;
        }
    }
}

inline std::string dump(const Value& v, int indent = 2) {
    std::string out;
    dump_into(v, out, indent, 0);
    out += '\n';
    return out;
}

// ---------------- 解析 ----------------
class Parser {
public:
    explicit Parser(const std::string& s) : s_(s) {}

    Value parse() {
        skip_ws();
        Value v = parse_value();
        skip_ws();
        if (pos_ != s_.size())
            throw std::runtime_error("json: trailing characters at " + std::to_string(pos_));
        return v;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) {
        throw std::runtime_error("json: " + msg + " at position " + std::to_string(pos_));
    }
    char peek() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_];
    }
    char get() { char c = peek(); ++pos_; return c; }
    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }
    void skip_ws() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    Value parse_value() {
        skip_ws();
        if (pos_ >= s_.size()) fail("unexpected end of input");
        char c = s_[pos_];
        if (c == '{') return parse_object();
        if (c == '[') return parse_array();
        if (c == '"') return Value(parse_string());
        if (c == 't' || c == 'f') return parse_bool();
        if (c == 'n') return parse_null();
        return parse_number();
    }

    Value parse_object() {
        expect('{');
        Value v = Value::make_object();
        skip_ws();
        if (peek() == '}') { get(); return v; }
        while (true) {
            skip_ws();
            if (peek() != '"') fail("expected string key");
            std::string key = parse_string();
            skip_ws();
            expect(':');
            Value val = parse_value();
            v.set(key, std::move(val));
            skip_ws();
            char c = get();
            if (c == '}') break;
            if (c != ',') fail("expected ',' or '}'");
        }
        return v;
    }

    Value parse_array() {
        expect('[');
        Value v = Value::make_array();
        skip_ws();
        if (peek() == ']') { get(); return v; }
        while (true) {
            Value val = parse_value();
            v.push_back(std::move(val));
            skip_ws();
            char c = get();
            if (c == ']') break;
            if (c != ',') fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        while (true) {
            char c = get();
            if (c == '"') break;
            if (c == '\\') {
                char e = get();
                switch (e) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'n': out += '\n'; break;
                    case 't': out += '\t'; break;
                    case 'r': out += '\r'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'u': {
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = get();
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= h - '0';
                            else if (h >= 'a' && h <= 'f') code |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') code |= h - 'A' + 10;
                            else fail("bad unicode escape");
                        }
                        // UTF-8 编码（代理对也做基本处理）
                        if (code < 0x80) out += static_cast<char>(code);
                        else if (code < 0x800) {
                            out += static_cast<char>(0xC0 | (code >> 6));
                            out += static_cast<char>(0x80 | (code & 0x3F));
                        } else {
                            out += static_cast<char>(0xE0 | (code >> 12));
                            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
                            out += static_cast<char>(0x80 | (code & 0x3F));
                        }
                        break;
                    }
                    default: fail("bad escape");
                }
            } else {
                out += c;
            }
        }
        return out;
    }

    Value parse_bool() {
        if (s_.compare(pos_, 4, "true") == 0) { pos_ += 4; return Value(true); }
        if (s_.compare(pos_, 5, "false") == 0) { pos_ += 5; return Value(false); }
        fail("invalid literal");
    }
    Value parse_null() {
        if (s_.compare(pos_, 4, "null") == 0) { pos_ += 4; return Value(); }
        fail("invalid literal");
    }
    Value parse_number() {
        size_t start = pos_;
        if (peek() == '-' || peek() == '+') get();
        bool any = false;
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                ++pos_;
                any = true;
            } else break;
        }
        if (!any) fail("invalid number");
        std::string token = s_.substr(start, pos_ - start);
        try {
            return Value(std::stod(token));
        } catch (...) {
            fail("bad number '" + token + "'");
        }
    }
};

inline Value parse(const std::string& text) {
    Parser p(text);
    return p.parse();
}

} // namespace mini_json
