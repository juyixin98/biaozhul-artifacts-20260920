// json.hpp — minimal self-contained JSON parser/serializer.
//
// Only what this project needs: objects (insertion-ordered), arrays, numbers
// (double), strings (with \uXXXX escapes), booleans, null. Numbers serialize
// with %.17g so a double round-trips exactly and output is deterministic.
#pragma once

#include <cctype>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace simp {

struct JsonError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct Json {
    enum class Type { Null, Bool, Num, Str, Arr, Obj };

    Type type = Type::Null;
    bool boolean = false;
    double num = 0.0;
    std::string str;
    std::vector<Json> arr;
    std::vector<std::pair<std::string, Json>> obj;

    static Json make_bool(bool b) { Json j; j.type = Type::Bool; j.boolean = b; return j; }
    static Json make_num(double v) { Json j; j.type = Type::Num; j.num = v; return j; }
    static Json make_str(std::string s) { Json j; j.type = Type::Str; j.str = std::move(s); return j; }
    static Json make_arr() { Json j; j.type = Type::Arr; return j; }
    static Json make_obj() { Json j; j.type = Type::Obj; return j; }

    const Json* find(const std::string& key) const {
        if (type != Type::Obj) return nullptr;
        for (const auto& kv : obj) {
            if (kv.first == key) return &kv.second;
        }
        return nullptr;
    }

    void set(const std::string& key, Json value) {
        type = Type::Obj;
        for (auto& kv : obj) {
            if (kv.first == key) { kv.second = std::move(value); return; }
        }
        obj.emplace_back(key, std::move(value));
    }
};

class JsonParser {
public:
    explicit JsonParser(const std::string& text) : s_(text) {}

    Json parse() {
        skip_ws();
        Json v = parse_value();
        skip_ws();
        if (pos_ != s_.size()) fail("trailing characters after top-level value");
        return v;
    }

private:
    const std::string& s_;
    std::size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw JsonError("JSON parse error at byte " + std::to_string(pos_) + ": " + msg);
    }

    void skip_ws() {
        while (pos_ < s_.size() &&
               (s_[pos_] == ' ' || s_[pos_] == '\t' || s_[pos_] == '\n' || s_[pos_] == '\r')) {
            ++pos_;
        }
    }

    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }

    void expect(char c) {
        if (peek() != c) fail(std::string("expected '") + c + "'");
        ++pos_;
    }

    Json parse_value() {
        switch (peek()) {
            case '{': return parse_object();
            case '[': return parse_array();
            case '"': { Json j; j.type = Json::Type::Str; j.str = parse_string(); return j; }
            case 't': literal("true"); return Json::make_bool(true);
            case 'f': literal("false"); return Json::make_bool(false);
            case 'n': literal("null"); return Json{};
            default: return parse_number();
        }
    }

    void literal(const char* word) {
        for (const char* p = word; *p; ++p) {
            if (peek() != *p) fail("invalid literal");
            ++pos_;
        }
    }

    Json parse_object() {
        Json j = Json::make_obj();
        expect('{');
        skip_ws();
        if (peek() == '}') { ++pos_; return j; }
        while (true) {
            skip_ws();
            if (peek() != '"') fail("expected object key string");
            std::string key = parse_string();
            skip_ws();
            expect(':');
            skip_ws();
            j.obj.emplace_back(std::move(key), parse_value());
            skip_ws();
            if (peek() == ',') { ++pos_; continue; }
            if (peek() == '}') { ++pos_; return j; }
            fail("expected ',' or '}' in object");
        }
    }

    Json parse_array() {
        Json j = Json::make_arr();
        expect('[');
        skip_ws();
        if (peek() == ']') { ++pos_; return j; }
        while (true) {
            skip_ws();
            j.arr.push_back(parse_value());
            skip_ws();
            if (peek() == ',') { ++pos_; continue; }
            if (peek() == ']') { ++pos_; return j; }
            fail("expected ',' or ']' in array");
        }
    }

    static void append_utf8(std::string& out, unsigned code) {
        if (code < 0x80) {
            out.push_back(static_cast<char>(code));
        } else if (code < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (code >> 6)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xE0 | (code >> 12)));
            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
        }
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        while (true) {
            if (pos_ >= s_.size()) fail("unterminated string");
            char c = s_[pos_++];
            if (c == '"') return out;
            if (c == '\\') {
                if (pos_ >= s_.size()) fail("unterminated escape");
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
                        if (pos_ + 4 > s_.size()) fail("bad \\u escape");
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = s_[pos_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else fail("bad hex digit in \\u escape");
                        }
                        append_utf8(out, code);
                        break;
                    }
                    default: fail("unknown escape sequence");
                }
            } else {
                out.push_back(c);
            }
        }
    }

    Json parse_number() {
        const std::size_t start = pos_;
        if (peek() == '-') ++pos_;
        if (!std::isdigit(static_cast<unsigned char>(peek()))) fail("invalid number");
        if (peek() == '0') {
            ++pos_;
        } else {
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == '.') {
            ++pos_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) fail("invalid fraction");
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) fail("invalid exponent");
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        Json j;
        j.type = Json::Type::Num;
        j.num = std::stod(s_.substr(start, pos_ - start));
        return j;
    }
};

inline Json parse_json(const std::string& text) { return JsonParser(text).parse(); }

inline void json_escape_into(std::string& out, const std::string& s) {
    out.push_back('"');
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(c);
                }
        }
    }
    out.push_back('"');
}

inline std::string json_serialize(const Json& j, bool pretty = false, int indent = 0) {
    const std::string pad = pretty ? std::string(static_cast<std::size_t>(indent), ' ') : "";
    const std::string nl = pretty ? "\n" : "";
    const std::string sp = pretty ? " " : "";
    switch (j.type) {
        case Json::Type::Null: return "null";
        case Json::Type::Bool: return j.boolean ? "true" : "false";
        case Json::Type::Num: {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.17g", j.num);
            return buf;
        }
        case Json::Type::Str: {
            std::string out;
            json_escape_into(out, j.str);
            return out;
        }
        case Json::Type::Arr: {
            if (j.arr.empty()) return "[]";
            std::string out = "[";
            for (std::size_t i = 0; i < j.arr.size(); ++i) {
                if (i) out += ",";
                out += nl + (pretty ? std::string(static_cast<std::size_t>(indent) + 2, ' ') : "");
                out += json_serialize(j.arr[i], pretty, indent + 2);
            }
            out += nl + pad + "]";
            return out;
        }
        case Json::Type::Obj: {
            if (j.obj.empty()) return "{}";
            std::string out = "{";
            for (std::size_t i = 0; i < j.obj.size(); ++i) {
                if (i) out += ",";
                out += nl + (pretty ? std::string(static_cast<std::size_t>(indent) + 2, ' ') : "");
                json_escape_into(out, j.obj[i].first);
                out += ":" + sp + json_serialize(j.obj[i].second, pretty, indent + 2);
            }
            out += nl + pad + "}";
            return out;
        }
    }
    return "null";
}

} // namespace simp
