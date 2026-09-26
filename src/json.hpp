// Minimal JSON parser/serializer (RFC 8259 subset sufficient for this API).
// No external dependencies. Values own their data.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace json {

class Value {
public:
    enum Type { Null, Bool, Int, Double, Str, Arr, Obj };

    Type type = Null;
    bool boolean = false;
    long long integer = 0;
    double real = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::map<std::string, Value> obj;

    Value() = default;
    static Value makeInt(long long v) { Value x; x.type = Int; x.integer = v; return x; }
    static Value makeDouble(double v) { Value x; x.type = Double; x.real = v; return x; }
    static Value makeStr(std::string v) { Value x; x.type = Str; x.str = std::move(v); return x; }
    static Value makeBool(bool v) { Value x; x.type = Bool; x.boolean = v; return x; }
    static Value makeArr() { Value x; x.type = Arr; return x; }
    static Value makeObj() { Value x; x.type = Obj; return x; }

    const Value* find(const std::string& key) const {
        if (type != Obj) return nullptr;
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }
};

class Parser {
public:
    Parser(const std::string& input) : s_(input) {}

    bool parse(Value& out, std::string& err) {
        skipWs();
        if (!parseValue(out)) { err = fail("invalid JSON value"); return false; }
        skipWs();
        if (pos_ != s_.size()) { err = fail("trailing characters"); return false; }
        return true;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;

    std::string fail(const std::string& msg) const {
        return msg + " at offset " + std::to_string(pos_);
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

    bool parseValue(Value& v) {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject(v);
            case '[': return parseArray(v);
            case '"': {
                std::string tmp;
                if (!parseString(tmp)) return false;
                v = Value::makeStr(std::move(tmp));
                return true;
            }
            case 't': return parseLit("true", Value::makeBool(true), v);
            case 'f': return parseLit("false", Value::makeBool(false), v);
            case 'n': return parseLit("null", Value(), v);
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber(v);
                return false;
        }
    }

    bool parseLit(const char* lit, const Value& val, Value& out) {
        size_t len = std::char_traits<char>::length(lit);
        if (s_.compare(pos_, len, lit) != 0) return false;
        pos_ += len;
        out = val;
        return true;
    }

    bool parseNumber(Value& v) {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        if (peek() == '0') ++pos_;
        else if (peek() >= '1' && peek() <= '9') { while (peek() >= '0' && peek() <= '9') ++pos_; }
        else return false;

        bool isDouble = false;
        if (peek() == '.') {
            isDouble = true;
            ++pos_;
            if (!(peek() >= '0' && peek() <= '9')) return false;
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            isDouble = true;
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (!(peek() >= '0' && peek() <= '9')) return false;
            while (peek() >= '0' && peek() <= '9') ++pos_;
        }
        std::string token = s_.substr(start, pos_ - start);
        try {
            if (isDouble) {
                v = Value::makeDouble(std::stod(token));
            } else {
                v = Value::makeInt(std::stoll(token));
            }
        } catch (...) {
            return false;
        }
        return true;
    }

    bool parseString(std::string& out) {
        if (!consume('"')) return false;
        while (true) {
            if (pos_ >= s_.size()) return false;
            char c = s_[pos_++];
            if (c == '"') return true;
            if (c < 0x20) return false;
            if (c == '\\') {
                if (pos_ >= s_.size()) return false;
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
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            if (pos_ >= s_.size()) return false;
                            char h = s_[pos_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= h - '0';
                            else if (h >= 'a' && h <= 'f') code |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') code |= h - 'A' + 10;
                            else return false;
                        }
                        appendUtf8(out, code);
                        break;
                    }
                    default: return false;
                }
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
        } else {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }

    bool parseArray(Value& v) {
        if (!consume('[')) return false;
        v = Value::makeArr();
        skipWs();
        if (consume(']')) return true;
        while (true) {
            Value item;
            if (!parseValue(item)) return false;
            v.arr.push_back(std::move(item));
            skipWs();
            if (consume(',')) { skipWs(); continue; }
            return consume(']');
        }
    }

    bool parseObject(Value& v) {
        if (!consume('{')) return false;
        v = Value::makeObj();
        skipWs();
        if (consume('}')) return true;
        while (true) {
            skipWs();
            std::string key;
            if (!parseString(key)) return false;
            skipWs();
            if (!consume(':')) return false;
            Value item;
            if (!parseValue(item)) return false;
            v.obj.emplace(std::move(key), std::move(item));
            skipWs();
            if (consume(',')) continue;
            return consume('}');
        }
    }
};

inline bool parse(const std::string& input, Value& out, std::string& err) {
    Parser p(input);
    return p.parse(out, err);
}

inline void dumpTo(const Value& v, std::string& out) {
    switch (v.type) {
        case Value::Null: out += "null"; break;
        case Value::Bool: out += v.boolean ? "true" : "false"; break;
        case Value::Int: out += std::to_string(v.integer); break;
        case Value::Double: {
            std::ostringstream ss;
            ss << v.real;
            out += ss.str();
            break;
        }
        case Value::Str: {
            out.push_back('"');
            for (unsigned char c : v.str) {
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
            break;
        }
        case Value::Arr: {
            out.push_back('[');
            for (size_t i = 0; i < v.arr.size(); ++i) {
                if (i) out.push_back(',');
                dumpTo(v.arr[i], out);
            }
            out.push_back(']');
            break;
        }
        case Value::Obj: {
            out.push_back('{');
            bool first = true;
            for (const auto& [k, val] : v.obj) {
                if (!first) out.push_back(',');
                first = false;
                out.push_back('"');
                out += k;
                out += "\":";
                dumpTo(val, out);
            }
            out.push_back('}');
            break;
        }
    }
}

inline std::string dump(const Value& v) {
    std::string out;
    dumpTo(v, out);
    return out;
}

} // namespace json
