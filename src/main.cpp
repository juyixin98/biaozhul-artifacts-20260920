// main.cpp — 轨迹时间插值服务的 JSON 请求入口(纯后端,仅输出坐标与数值)。
//
// 用法:
//   traj_interp [request.json]     从文件读请求;省略参数或传 "-" 时读 stdin。
//   结果 JSON 写 stdout。
//
// 请求格式:
//   {
//     "trajectory": [
//       {"t": 0.0, "position": [x,y,z], "orientation": [w,x,y,z]}, ...
//     ],
//     "queries": [0.5, 1.25, ...]
//   }
//
// 响应格式:
//   {"ok": true,
//    "frame": {"position_unit": "m", "angle": "quaternion(w,x,y,z)",
//              "time_unit": "s", "handedness": "right"},
//    "results": [{"t": ..., "ok": true, "position": [...], "orientation": [...]},
//                {"t": ..., "ok": false, "error": "out_of_range"}, ...]}
//   请求级错误:{"ok": false, "error": "<code>", "message": "<detail>"}
//
// 退出码:0 = 请求已处理(单个查询失败不影响);1 = IO/解析/请求非法。
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>

#include "traj/json.hpp"
#include "traj/trajectory.hpp"

namespace {

using traj::json::Value;

std::string readAll(std::istream& is) {
  std::ostringstream ss;
  ss << is.rdbuf();
  return ss.str();
}

Value errorResponse(const std::string& code, const std::string& msg) {
  Value root = Value::makeObject();
  root.obj["ok"] = Value::makeBool(false);
  root.obj["error"] = Value::makeString(code);
  root.obj["message"] = Value::makeString(msg);
  return root;
}

// 解析 [x, y, z] 或 [w, x, y, z] 数组为固定长度;失败抛 invalid_argument。
std::vector<double> parseVec(const Value& v, size_t n, const std::string& what) {
  if (!v.isArray() || v.arr.size() != n) {
    throw std::invalid_argument(what + " must be an array of " + std::to_string(n) + " numbers");
  }
  std::vector<double> out;
  out.reserve(n);
  for (const auto& e : v.arr) {
    if (!e.isNumber()) {
      throw std::invalid_argument(what + " elements must be numbers");
    }
    out.push_back(e.number);
  }
  return out;
}

Value frameInfo() {
  Value f = Value::makeObject();
  f.obj["position_unit"] = Value::makeString("m");
  f.obj["time_unit"] = Value::makeString("s");
  f.obj["orientation"] = Value::makeString("unit quaternion (w,x,y,z)");
  f.obj["handedness"] = Value::makeString("right");
  f.obj["extrapolation"] = Value::makeString("rejected (out_of_range)");
  return f;
}

Value handleRequest(const Value& req) {
  if (!req.isObject()) {
    return errorResponse("bad_request", "request must be a JSON object");
  }
  const Value* trajV = req.find("trajectory");
  const Value* queriesV = req.find("queries");
  if (!trajV || !trajV->isArray() || trajV->arr.empty()) {
    return errorResponse("bad_request", "'trajectory' must be a non-empty array of poses");
  }
  if (!queriesV || !queriesV->isArray()) {
    return errorResponse("bad_request", "'queries' must be an array of timestamps");
  }

  std::vector<traj::Pose> poses;
  try {
    for (size_t i = 0; i < trajV->arr.size(); ++i) {
      const Value& p = trajV->arr[i];
      const Value* t = p.find("t");
      const Value* pos = p.find("position");
      const Value* ori = p.find("orientation");
      if (!t || !t->isNumber()) {
        throw std::invalid_argument("pose " + std::to_string(i) + ": missing numeric 't'");
      }
      if (!pos) throw std::invalid_argument("pose " + std::to_string(i) + ": missing 'position'");
      if (!ori) throw std::invalid_argument("pose " + std::to_string(i) + ": missing 'orientation'");
      const auto pv = parseVec(*pos, 3, "position");
      const auto qv = parseVec(*ori, 4, "orientation");
      traj::Pose pose;
      pose.t = t->number;
      pose.position = {pv[0], pv[1], pv[2]};
      pose.orientation = {qv[0], qv[1], qv[2], qv[3]};
      poses.push_back(pose);
    }
  } catch (const std::invalid_argument& e) {
    return errorResponse("bad_request", e.what());
  }

  std::unique_ptr<traj::Trajectory> traj;
  try {
    traj = std::make_unique<traj::Trajectory>(std::move(poses));
  } catch (const std::invalid_argument& e) {
    return errorResponse("invalid_trajectory", e.what());
  }

  Value root = Value::makeObject();
  root.obj["ok"] = Value::makeBool(true);
  root.obj["frame"] = frameInfo();
  Value results = Value::makeArray();
  for (const auto& q : queriesV->arr) {
    Value item = Value::makeObject();
    if (!q.isNumber()) {
      item.obj["ok"] = Value::makeBool(false);
      item.obj["error"] = Value::makeString("non_numeric_query");
      results.arr.push_back(item);
      continue;
    }
    item.obj["t"] = Value::makeNumber(q.number);
    const traj::InterpResult r = traj->at(q.number);
    if (!r.ok) {
      item.obj["ok"] = Value::makeBool(false);
      item.obj["error"] = Value::makeString(r.error);
    } else {
      item.obj["ok"] = Value::makeBool(true);
      Value pos = Value::makeArray();
      pos.arr = {Value::makeNumber(r.pose.position.x), Value::makeNumber(r.pose.position.y),
                 Value::makeNumber(r.pose.position.z)};
      Value ori = Value::makeArray();
      ori.arr = {Value::makeNumber(r.pose.orientation.w), Value::makeNumber(r.pose.orientation.x),
                 Value::makeNumber(r.pose.orientation.y), Value::makeNumber(r.pose.orientation.z)};
      item.obj["position"] = pos;
      item.obj["orientation"] = ori;
    }
    results.arr.push_back(item);
  }
  root.obj["results"] = results;
  return root;
}

}  // namespace

int main(int argc, char** argv) {
  std::string input;
  if (argc > 1 && std::string(argv[1]) != "-") {
    std::ifstream f(argv[1]);
    if (!f) {
      std::cout << traj::json::dump(errorResponse("io_error",
                                                  std::string("cannot open file: ") + argv[1]))
                << "\n";
      return 1;
    }
    input = readAll(f);
  } else {
    input = readAll(std::cin);
  }

  Value req;
  try {
    req = traj::json::parse(input);
  } catch (const std::invalid_argument& e) {
    std::cout << traj::json::dump(errorResponse("parse_error", e.what())) << "\n";
    return 1;
  }

  const Value resp = handleRequest(req);
  std::cout << traj::json::dump(resp) << "\n";
  const Value* ok = resp.find("ok");
  return (ok && ok->type == Value::Type::Bool && ok->boolean) ? 0 : 1;
}
