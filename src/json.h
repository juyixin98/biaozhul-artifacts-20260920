// json.h — minimal JSON parser/serializer (sufficient for the registration API).
#pragma once
#include <cstdint>
#include <cmath>
#include <cstdio>
#include <limits>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace pcr {

class Json {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    Json() : type_(Type::Null) {}
    static Json makeBool(bool b) { Json j; j.type_ = Type::Bool; j.bool_ = b; return j; }
    static Json makeNumber(double d) { Json j; j.type_ = Type::Number; j.num_ = d; return j; }
    static Json makeString(std::string s) { Json j; j.type_ = Type::String; j.str_ = std::move(s); return j; }
    static Json makeArray() { Json j; j.type_ = Type::Array; return j; }
    static Json makeObject() { Json j; j.type_ = Type::Object; return j; }

    Type type() const { return type_; }
    bool isNull() const { return type_ == Type::Null; }
    bool isObject() const { return type_ == Type::Object; }
    bool isArray() const { return type_ == Type::Array; }
    bool isNumber() const { return type_ == Type::Number; }
    bool isString() const { return type_ == Type::String; }
    bool isBool() const { return type_ == Type::Bool; }

    bool asBool() const { check(Type::Bool); return bool_; }
    double asNumber() const { check(Type::Number); return num_; }
    const std::string& asString() const { check(Type::String); return str_; }
    const std::vector<Json>& asArray() const { check(Type::Array); return arr_; }
    std::vector<Json>& asArray() { check(Type::Array); return arr_; }

    bool contains(const std::string& k) const { return type_ == Type::Object && fields_.count(k) > 0; }
    const Json& at(const std::string& k) const {
        check(Type::Object);
        auto it = fields_.find(k);
        if (it == fields_.end()) throw std::runtime_error("json: missing key '" + k + "'");
        return it->second;
    }

    void push(Json v) { check(Type::Array); arr_.push_back(std::move(v)); }
    void set(const std::string& k, Json v) {
        if (type_ != Type::Object) throw std::runtime_error("json: not an object");
        fields_[k] = std::move(v);
    }

    std::string dump() const {
        std::ostringstream os;
        write(os, this);
        return os.str();
    }

    static Json parse(const std::string& s) {
        size_t pos = 0;
        Json v = parseValue(s, pos);
        skipWs(s, pos);
        if (pos != s.size()) throw std::runtime_error("json: trailing characters at " + std::to_string(pos));
        return v;
    }

private:
    Type type_;
    bool bool_ = false;
    double num_ = 0.0;
    std::string str_;
    std::vector<Json> arr_;
    std::map<std::string, Json> fields_;

    void check(Type t) const {
        if (type_ != t) throw std::runtime_error("json: type mismatch");
    }

    static void skipWs(const std::string& s, size_t& p) {
        while (p < s.size() && (s[p] == ' ' || s[p] == '\t' || s[p] == '\n' || s[p] == '\r')) ++p;
    }

    static Json parseValue(const std::string& s, size_t& p) {
        skipWs(s, p);
        if (p >= s.size()) throw std::runtime_error("json: unexpected end");
        char c = s[p];
        if (c == '{') return parseObject(s, p);
        if (c == '[') return parseArray(s, p);
        if (c == '"') return Json::makeString(parseString(s, p));
        if (c == 't' || c == 'f') return parseBool(s, p);
        if (c == 'n' || c == 'N') {
            // "null" stays null; the JavaScript/Python NaN extension is parsed
            // as a non-finite number and later dropped by cleanCloud().
            if (s.compare(p, 3, "nan") == 0 || s.compare(p, 3, "NaN") == 0 ||
                s.compare(p, 3, "NAN") == 0) {
                p += 3;
                return Json::makeNumber(std::nan(""));
            }
            return parseNull(s, p);
        }
        if ((c == 'I' && s.compare(p, 8, "Infinity") == 0)) {
            p += 8;
            return Json::makeNumber(std::numeric_limits<double>::infinity());
        }
        if (c == '-' && p + 1 < s.size() && s[p + 1] == 'I' &&
            s.compare(p, 9, "-Infinity") == 0) {
            p += 9;
            return Json::makeNumber(-std::numeric_limits<double>::infinity());
        }
        return parseNumber(s, p);
    }

    static Json parseObject(const std::string& s, size_t& p) {
        Json o = Json::makeObject();
        ++p;  // {
        skipWs(s, p);
        if (p < s.size() && s[p] == '}') { ++p; return o; }
        while (true) {
            skipWs(s, p);
            if (p >= s.size() || s[p] != '"') throw std::runtime_error("json: expected key at " + std::to_string(p));
            std::string k = parseString(s, p);
            skipWs(s, p);
            if (p >= s.size() || s[p] != ':') throw std::runtime_error("json: expected ':' at " + std::to_string(p));
            ++p;
            o.set(k, parseValue(s, p));
            skipWs(s, p);
            if (p >= s.size()) throw std::runtime_error("json: unterminated object");
            if (s[p] == ',') { ++p; continue; }
            if (s[p] == '}') { ++p; break; }
            throw std::runtime_error("json: expected ',' or '}' at " + std::to_string(p));
        }
        return o;
    }

    static Json parseArray(const std::string& s, size_t& p) {
        Json a = Json::makeArray();
        ++p;  // [
        skipWs(s, p);
        if (p < s.size() && s[p] == ']') { ++p; return a; }
        while (true) {
            a.push(parseValue(s, p));
            skipWs(s, p);
            if (p >= s.size()) throw std::runtime_error("json: unterminated array");
            if (s[p] == ',') { ++p; continue; }
            if (s[p] == ']') { ++p; break; }
            throw std::runtime_error("json: expected ',' or ']' at " + std::to_string(p));
        }
        return a;
    }

    static std::string parseString(const std::string& s, size_t& p) {
        ++p;  // opening quote
        std::string out;
        while (p < s.size()) {
            char c = s[p++];
            if (c == '"') return out;
            if (c == '\\') {
                if (p >= s.size()) break;
                char e = s[p++];
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
                        if (p + 4 > s.size()) throw std::runtime_error("json: bad \\u escape");
                        unsigned cp = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = s[p++];
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp |= h - '0';
                            else if (h >= 'a' && h <= 'f') cp |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') cp |= h - 'A' + 10;
                            else throw std::runtime_error("json: bad hex digit");
                        }
                        // Minimal UTF-8 encoder (surrogate pairs are not needed for this API).
                        if (cp < 0x80) out += char(cp);
                        else if (cp < 0x800) {
                            out += char(0xC0 | (cp >> 6));
                            out += char(0x80 | (cp & 0x3F));
                        } else {
                            out += char(0xE0 | (cp >> 12));
                            out += char(0x80 | ((cp >> 6) & 0x3F));
                            out += char(0x80 | (cp & 0x3F));
                        }
                        break;
                    }
                    default: throw std::runtime_error("json: bad escape");
                }
            } else {
                out += c;
            }
        }
        throw std::runtime_error("json: unterminated string");
    }

    static Json parseBool(const std::string& s, size_t& p) {
        if (s.compare(p, 4, "true") == 0) { p += 4; return Json::makeBool(true); }
        if (s.compare(p, 5, "false") == 0) { p += 5; return Json::makeBool(false); }
        throw std::runtime_error("json: bad literal");
    }

    static Json parseNull(const std::string& s, size_t& p) {
        if (s.compare(p, 4, "null") == 0) { p += 4; return Json(); }
        throw std::runtime_error("json: bad literal");
    }

    static Json parseNumber(const std::string& s, size_t& p) {
        size_t start = p;
        if (p < s.size() && (s[p] == '-' || s[p] == '+')) ++p;
        bool any = false;
        while (p < s.size() &&
               ((s[p] >= '0' && s[p] <= '9') || s[p] == '.' || s[p] == 'e' ||
                s[p] == 'E' || s[p] == '+' || s[p] == '-')) {
            if (s[p] >= '0' && s[p] <= '9') any = true;
            ++p;
        }
        if (!any) throw std::runtime_error("json: bad number at " + std::to_string(start));
        try {
            return Json::makeNumber(std::stod(s.substr(start, p - start)));
        } catch (...) {
            throw std::runtime_error("json: bad number");
        }
    }

    static void write(std::ostringstream& os, const Json* j) {
        switch (j->type_) {
            case Type::Null: os << "null"; break;
            case Type::Bool: os << (j->bool_ ? "true" : "false"); break;
            case Type::Number: {
                double v = j->num_;
                if (v == static_cast<long long>(v) && std::abs(v) < 1e15)
                    os << static_cast<long long>(v);
                else {
                    std::ostringstream tmp;
                    tmp.precision(17);
                    tmp << v;
                    os << tmp.str();
                }
                break;
            }
            case Type::String: writeString(os, j->str_); break;
            case Type::Array: {
                os << '[';
                for (size_t i = 0; i < j->arr_.size(); ++i) {
                    if (i) os << ',';
                    write(os, &j->arr_[i]);
                }
                os << ']';
                break;
            }
            case Type::Object: {
                os << '{';
                bool first = true;
                for (const auto& kv : j->fields_) {
                    if (!first) os << ',';
                    first = false;
                    writeString(os, kv.first);
                    os << ':';
                    write(os, &kv.second);
                }
                os << '}';
                break;
            }
        }
    }

    static void writeString(std::ostringstream& os, const std::string& s) {
        os << '"';
        for (unsigned char c : s) {
            switch (c) {
                case '"': os << "\\\""; break;
                case '\\': os << "\\\\"; break;
                case '\b': os << "\\b"; break;
                case '\f': os << "\\f"; break;
                case '\n': os << "\\n"; break;
                case '\r': os << "\\r"; break;
                case '\t': os << "\\t"; break;
                default:
                    if (c < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                        os << buf;
                    } else {
                        os << char(c);
                    }
            }
        }
        os << '"';
    }
};

}  // namespace pcr
