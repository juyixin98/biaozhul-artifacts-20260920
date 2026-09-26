// Minimal dependency-free JSON parser/serializer.
// Objects preserve insertion order; integers are stored separately from doubles.
#ifndef MINIJSON_HPP
#define MINIJSON_HPP

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace minijson {

struct Value {
    enum Type { NUL, BOOL, INT, NUM, STR, ARR, OBJ } type = NUL;
    bool b = false;
    long long i = 0;
    double d = 0.0;
    std::string s;
    std::vector<Value> arr;
    std::vector<std::pair<std::string, Value>> obj;

    static Value makeNull() { Value v; v.type = NUL; return v; }
    static Value makeBool(bool x) { Value v; v.type = BOOL; v.b = x; return v; }
    static Value makeInt(long long x) { Value v; v.type = INT; v.i = x; return v; }
    static Value makeNum(double x) { Value v; v.type = NUM; v.d = x; return v; }
    static Value makeStr(std::string x) { Value v; v.type = STR; v.s = std::move(x); return v; }
    static Value makeArr() { Value v; v.type = ARR; return v; }
    static Value makeObj() { Value v; v.type = OBJ; return v; }

    const Value* find(const std::string& key) const;
    Value* find(const std::string& key);
    void set(const std::string& key, Value v);
};

// Pretty printer. indent <= 0 produces compact output.
std::string dump(const Value& v, int indent = 2);

struct ParseError {
    std::string message;
    size_t pos = 0;
};

bool parse(const std::string& text, Value& out, std::string& error);

}  // namespace minijson

#endif
