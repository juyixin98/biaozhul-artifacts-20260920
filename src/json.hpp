#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace json {

class Value {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    using Member = std::pair<std::string, Value>;

    Value() : type_(Type::Null), bool_(false), number_(0.0) {}
    Value(bool b) : type_(Type::Bool), bool_(b), number_(0.0) {}
    Value(double d) : type_(Type::Number), bool_(false), number_(d) {}
    Value(int i) : type_(Type::Number), bool_(false), number_(static_cast<double>(i)) {}
    Value(int64_t i) : type_(Type::Number), bool_(false), number_(static_cast<double>(i)) {}
    Value(const char* s) : type_(Type::String), bool_(false), number_(0.0), str_(s) {}
    Value(const std::string& s) : type_(Type::String), bool_(false), number_(0.0), str_(s) {}

    static Value array() { Value v; v.type_ = Type::Array; return v; }
    static Value object() { Value v; v.type_ = Type::Object; return v; }

    Type type() const { return type_; }
    bool isNull() const { return type_ == Type::Null; }
    bool isBool() const { return type_ == Type::Bool; }
    bool isNumber() const { return type_ == Type::Number; }
    bool isString() const { return type_ == Type::String; }
    bool isArray() const { return type_ == Type::Array; }
    bool isObject() const { return type_ == Type::Object; }

    bool asBool() const { return bool_; }
    double asNumber() const { return number_; }
    int64_t asInt() const { return static_cast<int64_t>(number_); }
    const std::string& asString() const { return str_; }

    const std::vector<Value>& asArray() const { return arr_; }
    const std::vector<Member>& asObject() const { return obj_; }

    std::vector<Value>& arrayItems() { return arr_; }
    std::vector<Member>& objectMembers() { return obj_; }

    void push_back(const Value& v) { arr_.push_back(v); }
    void set(const std::string& key, const Value& v) {
        for (auto& m : obj_) {
            if (m.first == key) { m.second = v; return; }
        }
        obj_.emplace_back(key, v);
    }
    const Value* find(const std::string& key) const {
        for (const auto& m : obj_) {
            if (m.first == key) return &m.second;
        }
        return nullptr;
    }

private:
    Type type_;
    bool bool_;
    double number_;
    std::string str_;
    std::vector<Value> arr_;
    std::vector<Member> obj_;
};

class ParseError : public std::runtime_error {
public:
    ParseError(const std::string& msg, size_t offset)
        : std::runtime_error(msg + " (offset " + std::to_string(offset) + ")"), offset_(offset) {}
    size_t offset() const { return offset_; }
private:
    size_t offset_;
};

// Parses a single JSON value. Leading/trailing whitespace is allowed; trailing
// non-whitespace characters after the value cause a ParseError.
Value parse(const std::string& text);

// Serializes a JSON value. indent=false produces compact single-line output.
std::string dump(const Value& v, bool indent = false);

}  // namespace json
