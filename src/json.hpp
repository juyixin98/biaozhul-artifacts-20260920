// json.hpp — minimal JSON value, parser and serializer.
//
// Only what the request/response protocol needs: objects, arrays, strings,
// integers (int64; fractional/exponent numbers are rejected), booleans and
// null. Parse errors throw JsonError with a human-readable message.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

struct JsonError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

class Json {
public:
    enum class Type { Null, Bool, Int, String, Array, Object };

    Json() : type_(Type::Null) {}
    explicit Json(bool b) : type_(Type::Bool), bool_(b) {}
    explicit Json(int64_t i) : type_(Type::Int), int_(i) {}
    explicit Json(std::string s) : type_(Type::String), str_(std::move(s)) {}
    explicit Json(std::vector<Json> a) : type_(Type::Array), arr_(std::move(a)) {}
    explicit Json(std::map<std::string, Json> o) : type_(Type::Object), obj_(std::move(o)) {}

    Type type() const { return type_; }
    bool isNull() const { return type_ == Type::Null; }
    bool isBool() const { return type_ == Type::Bool; }
    bool isInt() const { return type_ == Type::Int; }
    bool isString() const { return type_ == Type::String; }
    bool isArray() const { return type_ == Type::Array; }
    bool isObject() const { return type_ == Type::Object; }

    bool asBool() const { return bool_; }
    int64_t asInt() const { return int_; }
    const std::string& asString() const { return str_; }
    const std::vector<Json>& asArray() const { return arr_; }
    const std::map<std::string, Json>& asObject() const { return obj_; }

    // Returns nullptr when the key is absent.
    const Json* find(const std::string& key) const;

    static Json parse(const std::string& text);  // throws JsonError
    std::string dump() const;

private:
    Type type_;
    bool bool_ = false;
    int64_t int_ = 0;
    std::string str_;
    std::vector<Json> arr_;
    std::map<std::string, Json> obj_;
};
