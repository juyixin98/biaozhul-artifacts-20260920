#include "json.hpp"

#include <cctype>
#include <sstream>

namespace segint {

const JVal& JVal::at(const std::string& key) const {
    if (type != Object) throw JError("expected object");
    auto it = obj.find(key);
    if (it == obj.end()) throw JError("missing key: " + key);
    return it->second;
}

const std::string& JVal::as_string() const {
    if (type != String) throw JError("expected string");
    return str;
}

int64_t JVal::as_int() const {
    if (type != Int) throw JError("expected integer");
    return i;
}

const std::vector<JVal>& JVal::as_array() const {
    if (type != Array) throw JError("expected array");
    return arr;
}

const std::map<std::string, JVal>& JVal::as_object() const {
    if (type != Object) throw JError("expected object");
    return obj;
}

namespace {

class Parser {
public:
    explicit Parser(const std::string& t) : s(t) {}

    JVal parse() {
        skip_ws();
        JVal v = parse_value();
        skip_ws();
        if (pos != s.size()) fail("trailing characters");
        return v;
    }

private:
    const std::string& s;
    size_t pos = 0;

    [[noreturn]] void fail(const std::string& msg) {
        throw JError("JSON parse error at offset " + std::to_string(pos) + ": " + msg);
    }

    char peek() {
        if (pos >= s.size()) fail("unexpected end of input");
        return s[pos];
    }

    void skip_ws() {
        while (pos < s.size()) {
            char ch = s[pos];
            if (ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r') ++pos;
            else break;
        }
    }

    bool consume_lit(const char* lit) {
        size_t n = 0;
        while (lit[n]) ++n;
        if (s.compare(pos, n, lit) == 0) { pos += n; return true; }
        return false;
    }

    JVal parse_value() {
        skip_ws();
        if (pos >= s.size()) fail("unexpected end of input");
        switch (s[pos]) {
            case '{': return parse_object();
            case '[': return parse_array();
            case '"': return JVal::make_string(parse_string());
            case 't':
                if (consume_lit("true")) return JVal::make_bool(true);
                fail("bad literal");
            case 'f':
                if (consume_lit("false")) return JVal::make_bool(false);
                fail("bad literal");
            case 'n':
                if (consume_lit("null")) return JVal::make_null();
                fail("bad literal");
            default:
                if (s[pos] == '-' || (s[pos] >= '0' && s[pos] <= '9'))
                    return parse_number();
                fail("unexpected character");
        }
    }

    JVal parse_object() {
        ++pos;  // '{'
        JVal v = JVal::make_object();
        skip_ws();
        if (peek() == '}') { ++pos; return v; }
        while (true) {
            skip_ws();
            if (peek() != '"') fail("expected string key");
            std::string key = parse_string();
            skip_ws();
            if (peek() != ':') fail("expected ':'");
            ++pos;
            JVal val = parse_value();
            v.obj[key] = std::move(val);
            skip_ws();
            char ch = peek();
            if (ch == ',') { ++pos; continue; }
            if (ch == '}') { ++pos; break; }
            fail("expected ',' or '}'");
        }
        return v;
    }

    JVal parse_array() {
        ++pos;  // '['
        JVal v = JVal::make_array();
        skip_ws();
        if (peek() == ']') { ++pos; return v; }
        while (true) {
            v.arr.push_back(parse_value());
            skip_ws();
            char ch = peek();
            if (ch == ',') { ++pos; continue; }
            if (ch == ']') { ++pos; break; }
            fail("expected ',' or ']'");
        }
        return v;
    }

    std::string parse_string() {
        ++pos;  // opening quote
        std::string out;
        while (true) {
            if (pos >= s.size()) fail("unterminated string");
            char ch = s[pos++];
            if (ch == '"') break;
            if (static_cast<unsigned char>(ch) < 0x20) fail("unescaped control char");
            if (ch != '\\') { out.push_back(ch); continue; }
            if (pos >= s.size()) fail("unterminated escape");
            char e = s[pos++];
            switch (e) {
                case '"':  out.push_back('"');  break;
                case '\\': out.push_back('\\'); break;
                case '/':  out.push_back('/');  break;
                case 'b':  out.push_back('\b'); break;
                case 'f':  out.push_back('\f'); break;
                case 'n':  out.push_back('\n'); break;
                case 'r':  out.push_back('\r'); break;
                case 't':  out.push_back('\t'); break;
                case 'u': {
                    if (pos + 4 > s.size()) fail("bad \\u escape");
                    unsigned code = 0;
                    for (int k = 0; k < 4; ++k) {
                        char h = s[pos++];
                        code <<= 4;
                        if (h >= '0' && h <= '9') code |= unsigned(h - '0');
                        else if (h >= 'a' && h <= 'f') code |= unsigned(h - 'a' + 10);
                        else if (h >= 'A' && h <= 'F') code |= unsigned(h - 'A' + 10);
                        else fail("bad hex digit");
                    }
                    // 以 UTF-8 编码输出（含 UTF-16 代理对处理）。
                    auto encode = [&](unsigned cp) {
                        if (cp < 0x80) {
                            out.push_back(char(cp));
                        } else if (cp < 0x800) {
                            out.push_back(char(0xC0 | (cp >> 6)));
                            out.push_back(char(0x80 | (cp & 0x3F)));
                        } else {
                            out.push_back(char(0xE0 | (cp >> 12)));
                            out.push_back(char(0x80 | ((cp >> 6) & 0x3F)));
                            out.push_back(char(0x80 | (cp & 0x3F)));
                        }
                    };
                    if (code >= 0xD800 && code <= 0xDBFF) {
                        if (pos + 6 <= s.size() && s[pos] == '\\' && s[pos + 1] == 'u') {
                            pos += 2;
                            unsigned lo = 0;
                            for (int k = 0; k < 4; ++k) {
                                char h = s[pos++];
                                lo <<= 4;
                                if (h >= '0' && h <= '9') lo |= unsigned(h - '0');
                                else if (h >= 'a' && h <= 'f') lo |= unsigned(h - 'a' + 10);
                                else if (h >= 'A' && h <= 'F') lo |= unsigned(h - 'A' + 10);
                                else fail("bad hex digit");
                            }
                            encode(0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00));
                        } else {
                            fail("expected low surrogate");
                        }
                    } else {
                        encode(code);
                    }
                    break;
                }
                default: fail("bad escape");
            }
        }
        return out;
    }

    JVal parse_number() {
        // 仅接受：-?(0|[1-9][0-9]*) ，拒绝浮点、指数与前导零。
        size_t start = pos;
        if (s[pos] == '-') ++pos;
        if (pos >= s.size()) fail("bad number");
        if (s[pos] == '0') {
            ++pos;
        } else if (s[pos] >= '1' && s[pos] <= '9') {
            while (pos < s.size() && std::isdigit(static_cast<unsigned char>(s[pos]))) ++pos;
        } else {
            fail("bad number");
        }
        if (pos < s.size()) {
            char ch = s[pos];
            if (ch == '.' || ch == 'e' || ch == 'E')
                fail("only integer numbers are accepted");
        }
        std::string token = s.substr(start, pos - start);
        try {
            size_t used = 0;
            long long v = std::stoll(token, &used, 10);
            if (used != token.size()) fail("integer out of int64 range");
            return JVal::make_int(v);
        } catch (...) {
            fail("integer out of int64 range");
        }
    }
};

void emit_string(std::ostringstream& o, const std::string& s) {
    o << '"';
    for (unsigned char ch : s) {
        switch (ch) {
            case '"':  o << "\\\""; break;
            case '\\': o << "\\\\"; break;
            case '\b': o << "\\b";  break;
            case '\f': o << "\\f";  break;
 case '\n': o << "\\n";  break;
            case '\r': o << "\\r";  break;
            case '\t': o << "\\t";  break;
            default:
                if (ch < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", ch);
                    o << buf;
                } else {
                    o << ch;
                }
        }
    }
    o << '"';
}

void emit(std::ostringstream& o, const JVal& v, int indent, int depth) {
    auto pad = [&](int d) {
        if (indent >= 0) {
            o << '\n';
            for (int k = 0; k < indent * d; ++k) o << ' ';
        }
    };
    switch (v.type) {
        case JVal::Null:   o << "null"; break;
        case JVal::Bool:   o << (v.boolean ? "true" : "false"); break;
        case JVal::Int:    o << v.i; break;
        case JVal::String: emit_string(o, v.str); break;
        case JVal::Array:
            if (v.arr.empty()) { o << "[]"; break; }
            o << '[';
            for (size_t k = 0; k < v.arr.size(); ++k) {
                if (k) o << ',';
                pad(depth + 1);
                emit(o, v.arr[k], indent, depth + 1);
            }
            pad(depth);
            o << ']';
            break;
        case JVal::Object:
            if (v.obj.empty()) { o << "{}"; break; }
            o << '{';
            size_t k = 0;
            for (const auto& kv : v.obj) {
                if (k++) o << ',';
                pad(depth + 1);
                emit_string(o, kv.first);
                o << (indent >= 0 ? ": " : ":");
                emit(o, kv.second, indent, depth + 1);
            }
            pad(depth);
            o << '}';
            break;
    }
}

}  // namespace

JVal json_parse(const std::string& text) {
    Parser p(text);
    return p.parse();
}

std::string json_dump(const JVal& v, int indent) {
    std::ostringstream o;
    emit(o, v, indent, 0);
    return o.str();
}

}  // namespace segint
