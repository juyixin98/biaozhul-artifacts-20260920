#include "model.hpp"

#include <unordered_map>
#include <unordered_set>

namespace diffc {

namespace {

[[noreturn]] void fail(const std::string& msg) { throw InputError{msg}; }

const Json& requireField(const Json& obj, const char* key) {
  const Json* v = obj.find(key);
  if (!v) fail(std::string("missing required field \"") + key + "\"");
  return *v;
}

std::int64_t requireInt(const Json& v, const std::string& what) {
  if (v.type != Json::Type::Int) fail(what + " must be an integer");
  return v.integer;
}

}  // namespace

Problem parseProblem(const Json& root) {
  if (root.type != Json::Type::Obj) fail("request must be a JSON object");

  Problem p;

  // --- variables ---
  const Json& vars = requireField(root, "variables");
  if (vars.type != Json::Type::Arr) fail("\"variables\" must be an array of names");
  if (vars.arr.empty()) fail("\"variables\" must not be empty");
  if (vars.arr.size() > kMaxVariables)
    fail("too many variables (limit " + std::to_string(kMaxVariables) + ")");

  std::unordered_map<std::string, int> index;
  for (const Json& v : vars.arr) {
    if (v.type != Json::Type::Str || v.str.empty())
      fail("variable names must be non-empty strings");
    if (index.count(v.str)) fail("duplicate variable name \"" + v.str + "\"");
    index[v.str] = static_cast<int>(p.varNames.size());
    p.varNames.push_back(v.str);
  }

  // --- constraints ---
  const Json& cons = requireField(root, "constraints");
  if (cons.type != Json::Type::Arr) fail("\"constraints\" must be an array");
  if (cons.arr.size() > kMaxConstraints)
    fail("too many constraints (limit " + std::to_string(kMaxConstraints) + ")");

  std::unordered_set<std::string> usedIds;
  for (size_t i = 0; i < cons.arr.size(); ++i) {
    const Json& cj = cons.arr[i];
    if (cj.type != Json::Type::Obj)
      fail("constraint #" + std::to_string(i) + " must be an object");

    Constraint c;
    if (const Json* id = cj.find("id")) {
      if (id->type != Json::Type::Str || id->str.empty())
        fail("constraint id must be a non-empty string");
      c.id = id->str;
    } else {
      c.id = "c" + std::to_string(i);
    }
    if (!usedIds.insert(c.id).second) fail("duplicate constraint id \"" + c.id + "\"");

    const Json& xj = requireField(cj, "var");
    const Json& yj = requireField(cj, "minus");
    if (xj.type != Json::Type::Str || yj.type != Json::Type::Str)
      fail("constraint \"var\"/\"minus\" must be variable name strings");
    auto xi = index.find(xj.str);
    auto yi = index.find(yj.str);
    if (xi == index.end()) fail("unknown variable \"" + xj.str + "\" in constraint " + c.id);
    if (yi == index.end()) fail("unknown variable \"" + yj.str + "\" in constraint " + c.id);
    c.x = xi->second;
    c.y = yi->second;

    c.c = requireInt(requireField(cj, "bound"), "constraint \"bound\"");
    if (c.c > kMaxAbsBound || c.c < -kMaxAbsBound)
      fail("bound of constraint " + c.id + " exceeds |c| <= " +
           std::to_string(kMaxAbsBound));

    p.constraints.push_back(std::move(c));
  }

  return p;
}

}  // namespace diffc
