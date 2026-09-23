// json.hpp - 无第三方依赖的极简 JSON 值、解析与序列化。
// 仅支持标准 JSON（RFC 8259 子集）：null / bool / number / string /
// array / object。数字统一存为 IEEE 754 double。
#pragma once

#include <map>
#include <memory>
#include <string>
#include <vector>

namespace cps::json {

class Value {
 public:
  enum class Type { Null, Bool, Number, String, Array, Object };

  Value() : type_(Type::Null) {}
  explicit Value(bool b) : type_(Type::Bool), bool_(b) {}
  explicit Value(double d) : type_(Type::Number), num_(d) {}
  // 必须在 bool 之外单独提供 const char*：否则字符串字面量走
  // “指针->bool”标准转换（优先于“指针->std::string”用户定义转换），
  // 会被错误地序列化为 true。
  Value(const char* s) : type_(Type::String), str_(s) {}
  explicit Value(std::string s) : type_(Type::String), str_(std::move(s)) {}

  // 容器必须深拷贝：shared_ptr 别名共享会让“先放入 map 再修改副本”
  // 通过同一底层容器改到 map 自身（曾导致自引用 bug）。
  Value(const Value& other) { CopyFrom(other); }
  Value& operator=(const Value& other) {
    if (this != &other) CopyFrom(other);
    return *this;
  }
  Value(Value&&) noexcept = default;
  Value& operator=(Value&&) noexcept = default;

  static Value Array() {
    Value v;
    v.type_ = Type::Array;
    v.arr_ = std::make_shared<std::vector<Value>>();
    return v;
  }
  static Value Object() {
    Value v;
    v.type_ = Type::Object;
    v.obj_ = std::make_shared<std::map<std::string, Value>>();
    return v;
  }

  Type type() const { return type_; }
  bool is_null() const { return type_ == Type::Null; }
  bool is_bool() const { return type_ == Type::Bool; }
  bool is_number() const { return type_ == Type::Number; }
  bool is_string() const { return type_ == Type::String; }
  bool is_array() const { return type_ == Type::Array; }
  bool is_object() const { return type_ == Type::Object; }

  bool as_bool() const { return bool_; }
  double as_number() const { return num_; }
  const std::string& as_string() const { return str_; }
  std::vector<Value>& as_array() { return *arr_; }
  const std::vector<Value>& as_array() const { return *arr_; }
  std::map<std::string, Value>& as_object() { return *obj_; }
  const std::map<std::string, Value>& as_object() const { return *obj_; }

  void push_back(Value v) { arr_->push_back(std::move(v)); }
  void set(const std::string& key, Value v) {
    // emplace 语义：键已存在则覆盖
    (*obj_)[key] = std::move(v);
  }
  const Value* find(const std::string& key) const {
    if (!is_object()) return nullptr;
    auto it = obj_->find(key);
    return it == obj_->end() ? nullptr : &it->second;
  }

 private:
  Type type_;
  bool bool_ = false;
  double num_ = 0.0;
  std::string str_;
  std::shared_ptr<std::vector<Value>> arr_;
  std::shared_ptr<std::map<std::string, Value>> obj_;

  void CopyFrom(const Value& other) {
    type_ = other.type_;
    bool_ = other.bool_;
    num_ = other.num_;
    str_ = other.str_;
    arr_.reset();
    obj_.reset();
    if (other.arr_) {
      arr_ = std::make_shared<std::vector<Value>>(*other.arr_);
    }
    if (other.obj_) {
      obj_ = std::make_shared<std::map<std::string, Value>>(*other.obj_);
    }
  }
};

// 解析 JSON 文本。成功返回 Value，失败返回 null Value 并把错误说明
// 写入 err（含大致字符位置）。
Value Parse(const std::string& text, std::string* err);

// 序列化为紧凑 JSON（无多余空白）。
// 数字使用 %.17g：保证所有 binary64 值可往返且最短性通常足够；
// 不输出 NaN/Infinity（标准 JSON 无此值，输出 null）。
std::string Dump(const Value& v);

}  // namespace cps::json
