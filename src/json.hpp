#ifndef SCC_JSON_HPP
#define SCC_JSON_HPP

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace scc {

// Error thrown on malformed JSON or type mismatches.
class JsonError : public std::runtime_error {
 public:
  explicit JsonError(const std::string& msg) : std::runtime_error(msg) {}
};

// Minimal self-contained JSON value: null, bool, int64, double, string,
// array, object. Object keys are kept in a std::map so serialized output has
// deterministic (sorted) key order.
class Json {
 public:
  enum class Type { kNull, kBool, kInt, kDouble, kString, kArray, kObject };

  using Array = std::vector<Json>;
  using Object = std::map<std::string, Json>;

  Json() : type_(Type::kNull) {}
  Json(std::nullptr_t) : type_(Type::kNull) {}
  Json(bool v) : type_(Type::kBool), bool_(v) {}
  Json(int v) : type_(Type::kInt), int_(v) {}
  Json(int64_t v) : type_(Type::kInt), int_(v) {}
  Json(double v) : type_(Type::kDouble), double_(v) {}
  Json(const char* v) : type_(Type::kString), str_(v) {}
  Json(std::string v) : type_(Type::kString), str_(std::move(v)) {}
  Json(Array v) : type_(Type::kArray), arr_(std::move(v)) {}
  Json(Object v) : type_(Type::kObject), obj_(std::move(v)) {}

  Type type() const { return type_; }
  bool isNull() const { return type_ == Type::kNull; }
  bool isBool() const { return type_ == Type::kBool; }
  bool isInt() const { return type_ == Type::kInt; }
  bool isDouble() const { return type_ == Type::kDouble; }
  bool isNumber() const { return type_ == Type::kInt || type_ == Type::kDouble; }
  bool isString() const { return type_ == Type::kString; }
  bool isArray() const { return type_ == Type::kArray; }
  bool isObject() const { return type_ == Type::kObject; }

  bool asBool() const {
    require(Type::kBool, "bool");
    return bool_;
  }
  int64_t asInt() const {
    require(Type::kInt, "int");
    return int_;
  }
  double asDouble() const {
    if (type_ == Type::kInt) return static_cast<double>(int_);
    require(Type::kDouble, "double");
    return double_;
  }
  const std::string& asString() const {
    require(Type::kString, "string");
    return str_;
  }
  const Array& asArray() const {
    require(Type::kArray, "array");
    return arr_;
  }
  const Object& asObject() const {
    require(Type::kObject, "object");
    return obj_;
  }

  // Returns nullptr when the key is absent.
  const Json* find(const std::string& key) const {
    if (type_ != Type::kObject) return nullptr;
    auto it = obj_.find(key);
    return it == obj_.end() ? nullptr : &it->second;
  }

  // Parses text; throws JsonError with line/column on malformed input.
  static Json parse(const std::string& text);

  // Serializes. indent < 0 produces compact output; otherwise pretty-prints
  // with the given number of spaces per level.
  std::string dump(int indent = -1) const;

 private:
  void require(Type t, const char* what) const {
    if (type_ != t) throw JsonError(std::string("expected JSON ") + what);
  }

  Type type_;
  bool bool_ = false;
  int64_t int_ = 0;
  double double_ = 0.0;
  std::string str_;
  Array arr_;
  Object obj_;
};

}  // namespace scc

#endif  // SCC_JSON_HPP
