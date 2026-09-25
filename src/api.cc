#include "api.h"

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <sstream>

#include "bvh.h"
#include "json.h"
#include "ray_aabb.h"

namespace raybox {

namespace {

JsonValue makeError(const std::string& code, const std::string& message) {
  JsonValue root = jsonObject();
  root.obj["ok"] = [] {
    JsonValue b;
    b.type = JsonValue::Type::Bool;
    b.boolean = false;
    return b;
  }();
  JsonValue err = jsonObject();
  JsonValue c;
  c.type = JsonValue::Type::String;
  c.str = code;
  JsonValue m;
  m.type = JsonValue::Type::String;
  m.str = message;
  err.obj["code"] = c;
  err.obj["message"] = m;
  root.obj["error"] = err;
  return root;
}

JsonValue jsonNum(double d) {
  JsonValue v;
  v.type = JsonValue::Type::Number;
  v.number = d;
  return v;
}

JsonValue jsonInt(int64_t i) {
  JsonValue v;
  v.type = JsonValue::Type::Integer;
  v.integer = i;
  return v;
}

JsonValue jsonStr(const std::string& s) {
  JsonValue v;
  v.type = JsonValue::Type::String;
  v.str = s;
  return v;
}

JsonValue jsonBool(bool b) {
  JsonValue v;
  v.type = JsonValue::Type::Bool;
  v.boolean = b;
  return v;
}

JsonValue vecToJson(const Vec3& v) {
  JsonValue a = jsonArray();
  a.arr.push_back(jsonNum(v.x));
  a.arr.push_back(jsonNum(v.y));
  a.arr.push_back(jsonNum(v.z));
  return a;
}

// Reads a [x,y,z] numeric array. On failure returns false and sets error.
bool readVec3(const JsonValue& v, const char* name, Vec3& out,
              std::string& error) {
  if (!v.isArray() || v.arr.size() != 3) {
    error = std::string("'") + name + "' must be an array of 3 numbers";
    return false;
  }
  double d[3];
  for (int i = 0; i < 3; ++i) {
    if (!v.arr[i].isNumber()) {
      error = std::string("'") + name + "' must contain only numbers";
      return false;
    }
    d[i] = (v.arr[i].type == JsonValue::Type::Integer)
               ? static_cast<double>(v.arr[i].integer)
               : v.arr[i].number;
    if (!std::isfinite(d[i])) {
      error =
          std::string("'") + name + "' contains a non-finite value";
      return false;
    }
  }
  out = Vec3(d[0], d[1], d[2]);
  return true;
}

JsonValue hitToJson(const Hit& h) {
  JsonValue o = jsonObject();
  o.obj["id"] = jsonInt(static_cast<int64_t>(h.id));
  o.obj["t"] = jsonNum(h.t);
  o.obj["exit_t"] = jsonNum(h.exit_t);
  o.obj["point"] = vecToJson(h.point);
  o.obj["normal"] = vecToJson(h.normal);
  return o;
}

}  // namespace

std::string handleRequest(const std::string& text) {
  auto started = std::chrono::steady_clock::now();

  JsonValue root;
  std::string err;
  if (!jsonParse(text, root, err))
    return jsonDump(makeError("invalid_json", err));

  if (!root.isObject())
    return jsonDump(makeError("invalid_request", "top-level value must be an object"));

  // --- ray ---------------------------------------------------------------
  const JsonValue* ray = root.find("ray");
  if (!ray || !ray->isObject())
    return jsonDump(makeError("invalid_request", "missing 'ray' object"));

  Vec3 origin, dir;
  const JsonValue* o = ray->find("origin");
  const JsonValue* d = ray->find("dir");
  if (!o)
    return jsonDump(makeError("invalid_request", "missing ray.origin"));
  if (!d)
    return jsonDump(makeError("invalid_request", "missing ray.dir"));
  if (!readVec3(*o, "ray.origin", origin, err))
    return jsonDump(makeError("invalid_request", err));
  if (!readVec3(*d, "ray.dir", dir, err))
    return jsonDump(makeError("invalid_request", err));

  if (dir.x == 0.0 && dir.y == 0.0 && dir.z == 0.0)
    return jsonDump(makeError(
        "invalid_request",
        "ray.dir must be non-zero (a zero ray has no traversal direction)"));

  // --- mode / flags ------------------------------------------------------
  std::string mode = "nearest";
  if (const JsonValue* m = root.find("mode")) {
    if (m->type != JsonValue::Type::String)
      return jsonDump(makeError("invalid_request", "'mode' must be a string"));
    mode = m->str;
    if (mode != "nearest" && mode != "all")
      return jsonDump(makeError("invalid_request",
                                "'mode' must be \"nearest\" or \"all\""));
  }

  bool useBvh = true;
  if (const JsonValue* u = root.find("use_bvh")) {
    if (u->type != JsonValue::Type::Bool)
      return jsonDump(makeError("invalid_request", "'use_bvh' must be boolean"));
    useBvh = u->boolean;
  }

  // --- boxes -------------------------------------------------------------
  const JsonValue* boxes = root.find("boxes");
  if (!boxes || !boxes->isArray())
    return jsonDump(makeError("invalid_request", "missing 'boxes' array"));

  std::vector<AABB> aabbs;
  aabbs.reserve(boxes->arr.size());
  for (size_t i = 0; i < boxes->arr.size(); ++i) {
    const JsonValue& b = boxes->arr[i];
    if (!b.isObject())
      return jsonDump(makeError(
          "invalid_request",
          "boxes[" + std::to_string(i) + "] must be an object"));
    const JsonValue* bmin = b.find("min");
    const JsonValue* bmax = b.find("max");
    if (!bmin || !bmax)
      return jsonDump(makeError(
          "invalid_request",
          "boxes[" + std::to_string(i) + "] requires 'min' and 'max'"));
    AABB ab;
    if (!readVec3(*bmin, "box.min", ab.min, err))
      return jsonDump(makeError("invalid_request",
                                "boxes[" + std::to_string(i) + "]: " + err));
    if (!readVec3(*bmax, "box.max", ab.max, err))
      return jsonDump(makeError("invalid_request",
                                "boxes[" + std::to_string(i) + "]: " + err));
    // Degenerate boxes (zero extents) are allowed; inverted boxes are not.
    for (int axis = 0; axis < 3; ++axis) {
      if (ab.min[axis] > ab.max[axis])
        return jsonDump(makeError(
            "degenerate_geometry",
            "boxes[" + std::to_string(i) +
                "]: min component must not exceed max component"));
    }
    aabbs.push_back(ab);
  }

  // --- execute -----------------------------------------------------------
  JsonValue resp = jsonObject();
  resp.obj["ok"] = jsonBool(true);
  resp.obj["mode"] = jsonStr(mode);
  resp.obj["use_bvh"] = jsonBool(useBvh && !aabbs.empty());

  if (mode == "nearest") {
    Hit h;
    bool hit = false;
    if (useBvh && !aabbs.empty()) {
      BVH tree(aabbs);
      JsonValue nc = jsonInt(static_cast<int64_t>(tree.nodeCount()));
      resp.obj["bvh_nodes"] = nc;
      hit = tree.nearest(origin, dir, h);
    } else {
      hit = bruteForceNearest(aabbs, origin, dir, h);
    }
    resp.obj["hit"] = jsonBool(hit);
    if (hit) resp.obj["nearest"] = hitToJson(h);
  } else {
    std::vector<Hit> hits;
    if (useBvh && !aabbs.empty()) {
      BVH tree(aabbs);
      resp.obj["bvh_nodes"] = jsonInt(static_cast<int64_t>(tree.nodeCount()));
      hits = tree.allHits(origin, dir);
    } else {
      hits = bruteForceAll(aabbs, origin, dir);
    }
    JsonValue arr = jsonArray();
    for (const Hit& h : hits) arr.arr.push_back(hitToJson(h));
    resp.obj["count"] = jsonInt(static_cast<int64_t>(hits.size()));
    resp.obj["hits"] = arr;
  }

  auto done = std::chrono::steady_clock::now();
  double micros =
      std::chrono::duration<double, std::micro>(done - started).count();
  // Timing is informational; %.3f (nanosecond resolution) avoids emitting
  // meaningless 17-digit double noise.
  char tbuf[32];
  std::snprintf(tbuf, sizeof(tbuf), "%.3f", micros);
  resp.obj["elapsed_us"] = jsonRaw(tbuf);
  return jsonDump(resp);
}

}  // namespace raybox
