// json.hpp — 极简 JSON 解析/序列化（仅覆盖本项目所需子集）。
//   支持：对象、数组、字符串、64 位整数、true/false/null。
//   数字只接受整数（拒绝小数点与指数），与“整数坐标”输入契约一致。
#pragma once

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace segint {

struct JError : public std::runtime_error {
    explicit JError(const std::string& msg) : std::runtime_error(msg) {}
};

class JVal {
public:
    enum Type { Null, Bool, Int, String, Array, Object } type = Null;

    bool boolean = false;
    int64_t i = 0;
    std::string str;
    std::vector<JVal> arr;
    std::map<std::string, JVal> obj;  // 对象（有序 map，便于输出稳定）

    static JVal make_object() { JVal v; v.type = Object; return v; }
    static JVal make_array()  { JVal v; v.type = Array;  return v; }
    static JVal make_string(std::string s) { JVal v; v.type = String; v.str = std::move(s); return v; }
    static JVal make_int(int64_t n) { JVal v; v.type = Int; v.i = n; return v; }
    static JVal make_bool(bool b) { JVal v; v.type = Bool; v.boolean = b; return v; }
    static JVal make_null() { JVal v; v.type = Null; return v; }

    JVal& set(const std::string& key, JVal v) {
        type = Object;
        obj[key] = std::move(v);
        return *this;
    }
    JVal& push(JVal v) {
        type = Array;
        arr.push_back(std::move(v));
        return *this;
    }

    // 带类型检查的访问器，缺失/类型不符抛 JError。
    const JVal& at(const std::string& key) const;
    bool has(const std::string& key) const { return type == Object && obj.count(key); }
    const std::string& as_string() const;
    int64_t as_int() const;
    const std::vector<JVal>& as_array() const;
    const std::map<std::string, JVal>& as_object() const;
};

// 解析 JSON 文本；语法错误抛 JError（含位置信息）。
JVal json_parse(const std::string& text);

// 序列化；indent < 0 为紧凑输出，否则按缩进美化。
std::string json_dump(const JVal& v, int indent = -1);

}  // namespace segint
