#pragma once
// Minimal deterministic JSON value: parser + canonical serializer.
// Objects keep keys sorted (std::map) so serialization is deterministic
// (used by the response checksum).
#include <cstdint>
#include <map>
#include <string>
#include <vector>
#include <stdexcept>

namespace topp {

class JsonValue {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    using Array = std::vector<JsonValue>;
    using Object = std::map<std::string, JsonValue>;

    JsonValue() : type_(Type::Null), num_(0.0), bool_(false) {}
    JsonValue(bool b) : type_(Type::Bool), num_(0.0), bool_(b) {}
    JsonValue(double d) : type_(Type::Number), num_(d), bool_(false) {}
    JsonValue(int i) : type_(Type::Number), num_(static_cast<double>(i)), bool_(false) {}
    JsonValue(int64_t i) : type_(Type::Number), num_(static_cast<double>(i)), bool_(false) {}
    JsonValue(const char* s) : type_(Type::String), num_(0.0), bool_(false), str_(s) {}
    JsonValue(const std::string& s) : type_(Type::String), num_(0.0), bool_(false), str_(s) {}

    static JsonValue array() { JsonValue v; v.type_ = Type::Array; return v; }
    static JsonValue object() { JsonValue v; v.type_ = Type::Object; return v; }

    Type type() const { return type_; }
    bool is(Type t) const { return type_ == t; }

    double asNumber() const { require(Type::Number); return num_; }
    bool asBool() const { require(Type::Bool); return bool_; }
    const std::string& asString() const { require(Type::String); return str_; }
    const Array& asArray() const { require(Type::Array); return arr_; }
    Array& asArray() { require(Type::Array); return arr_; }
    const Object& asObject() const { require(Type::Object); return obj_; }
    Object& asObject() { require(Type::Object); return obj_; }

    bool contains(const std::string& k) const {
        require(Type::Object); return obj_.find(k) != obj_.end();
    }
    const JsonValue& at(const std::string& k) const {
        require(Type::Object);
        auto it = obj_.find(k);
        if (it == obj_.end()) throw std::out_of_range("json key not found: " + k);
        return it->second;
    }

    // Serialize canonically: sorted object keys, no whitespace.
    std::string dump() const;

    static JsonValue parse(const std::string& text);

private:
    void require(Type t) const {
        if (type_ != t) throw std::runtime_error("JSON type mismatch");
    }

    Type type_;
    double num_;
    bool bool_;
    std::string str_;
    Array arr_;
    Object obj_;
};

} // namespace topp
