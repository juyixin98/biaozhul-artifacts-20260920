// main.cpp - JSON 请求入口（stdin 或文件参数），纯后端、无地图输出。
//
// 请求:
// {
//   "epsilon": <number, >= 0>,
//   "points": [ {"x": num, "y": num}, ... ]
// }
//
// 成功响应:
// {
//   "status": "ok",
//   "epsilon": ...,
//   "numerical_tolerance": ...,
//   "input_point_count": n,
//   "deduplicated_point_count": m,
//   "removed_duplicate_point_indices": [...],   // 原始下标
//   "simplified": [ {"x":..,"y":..,"source_index": k}, ... ],
//   "simplified_count": k,
//   "verification": {
//     "metric": "point_to_polyline_euclidean",
//     "bound": "one_sided",
//     "max_distance": ...,
//     "bound": epsilon,
//     "within_bound": true/false,
//     "points": [
//       {"index": i, "x":..,"y":..,"distance": d,
//        "nearest_segment_index": j, "within_bound": bool}, ...
//     ]
//   }
// }
//
// 错误响应: {"status":"error","error":{"code":"...","message":"..."}}
//
// 退出码: 0 成功；1 语义校验错误（HTTP 风格 400）；2 IO/JSON 解析错误。
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#include "geometry.hpp"
#include "json.hpp"
#include "simplification.hpp"

namespace {

using cps::Point;
using cps::json::Value;

std::string ReadAll(std::istream& in) {
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

Value MakeError(const std::string& code, const std::string& message) {
  Value e = Value::Object();
  e.set("code", Value(code));
  e.set("message", Value(message));
  Value root = Value::Object();
  root.set("status", Value("error"));
  root.set("error", e);
  return root;
}

bool IsFiniteNumber(const Value& v) {
  return v.is_number() && std::isfinite(v.as_number());
}

// 校验并抽取点数组。失败时填充 error_code/error_msg。
bool ExtractPoints(const Value& req, std::vector<Point>& points,
                   std::string& code, std::string& msg) {
  const Value* pts = req.find("points");
  if (!pts) {
    code = "missing_field";
    msg = "request must contain a 'points' array";
    return false;
  }
  if (!pts->is_array()) {
    code = "invalid_type";
    msg = "'points' must be an array of {\"x\",\"y\"} objects";
    return false;
  }
  for (std::size_t i = 0; i < pts->as_array().size(); ++i) {
    const Value& item = pts->as_array()[i];
    if (!item.is_object()) {
      code = "invalid_type";
      msg = "points[" + std::to_string(i) + "] must be an object";
      return false;
    }
    const Value* x = item.find("x");
    const Value* y = item.find("y");
    if (!x || !y) {
      code = "missing_field";
      msg = "points[" + std::to_string(i) + "] requires numeric 'x' and 'y'";
      return false;
    }
    if (!IsFiniteNumber(*x) || !IsFiniteNumber(*y)) {
      code = "invalid_value";
      msg = "points[" + std::to_string(i) +
            "] x/y must be finite numbers (NaN/Infinity rejected)";
      return false;
    }
    points.push_back(Point{x->as_number(), y->as_number()});
  }
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  std::string input;
  if (argc > 2) {
    std::fprintf(stderr,
                 "usage: %s [request.json]   (reads stdin when no file given)\n",
                 argv[0]);
    return 2;
  }
  if (argc == 2) {
    std::ifstream f(argv[1]);
    if (!f) {
      std::cout << cps::json::Dump(
                       MakeError("io_error",
                                 std::string("cannot open file: ") + argv[1]))
                << "\n";
      return 2;
    }
    input = ReadAll(f);
  } else {
    input = ReadAll(std::cin);
  }

  std::string parse_err;
  Value req = cps::json::Parse(input, &parse_err);
  if (!req.is_object()) {
    std::cout << cps::json::Dump(
                     MakeError("invalid_json",
                               parse_err.empty() ? "request is not a JSON object"
                                                 : parse_err))
              << "\n";
    return 2;
  }

  // ---- epsilon ----
  const Value* eps_v = req.find("epsilon");
  if (!eps_v) {
    std::cout << cps::json::Dump(
                     MakeError("missing_field", "request must contain 'epsilon'"))
              << "\n";
    return 1;
  }
  if (!eps_v->is_number() || !std::isfinite(eps_v->as_number())) {
    std::cout << cps::json::Dump(MakeError("invalid_value",
                                           "'epsilon' must be a finite number"))
              << "\n";
    return 1;
  }
  const double epsilon = eps_v->as_number();
  if (epsilon < 0.0) {
    std::cout << cps::json::Dump(
                     MakeError("invalid_value", "'epsilon' must be >= 0"))
              << "\n";
    return 1;
  }

  // ---- points ----
  std::vector<Point> points;
  std::string code, msg;
  if (!ExtractPoints(req, points, code, msg)) {
    std::cout << cps::json::Dump(MakeError(code, msg)) << "\n";
    return 1;
  }

  // ---- 去重（删除相邻重复点，消除零长段）----
  std::vector<std::size_t> removed_indices;
  std::vector<Point> dedup;
  dedup.reserve(points.size());
  for (std::size_t i = 0; i < points.size(); ++i) {
    if (dedup.empty() || !(dedup.back() == points[i])) {
      dedup.push_back(points[i]);
    } else {
      removed_indices.push_back(i);
    }
  }

  // ---- Douglas-Peucker ----
  cps::SimplifyResult sr = cps::DouglasPeucker(dedup, epsilon);

  // ---- 逐点误差验证：每个“原始输入点”到简化折线的距离 ----
  // 数值容差：距离由若干次 binary64 运算（乘加/hypot）得到，
  // 取与坐标尺度、epsilon 尺度成比例的舍入界。
  double coord_scale = 0.0;
  for (const Point& p : points) {
    coord_scale = std::max(coord_scale, std::fabs(p.x));
    coord_scale = std::max(coord_scale, std::fabs(p.y));
  }
  const double scale = std::max({epsilon, coord_scale, 1.0});
  const double tolerance = 16.0 * std::numeric_limits<double>::epsilon() * scale;

  Value per_point = Value::Array();
  double max_distance = 0.0;
  bool all_within = true;

  for (std::size_t i = 0; i < points.size(); ++i) {
    const Point& p = points[i];
    double d;
    long long nearest = -1;

    if (sr.kept.empty()) {
      d = 0.0;  // 空输入 -> 空简化折线；没有点需要检验
    } else if (sr.kept.size() == 1) {
      d = std::hypot(p.x - sr.kept[0].x, p.y - sr.kept[0].y);
      nearest = -1;  // 简化结果为单点，无线段
    } else {
      d = std::numeric_limits<double>::infinity();
      for (std::size_t j = 0; j + 1 < sr.kept.size(); ++j) {
        const double dj =
            cps::PointToSegmentDistance(p, sr.kept[j], sr.kept[j + 1]);
        if (dj < d) {  // 并列取最小线段下标，保证确定性
          d = dj;
          nearest = static_cast<long long>(j);
        }
      }
    }

    const bool within = (d <= epsilon + tolerance);
    all_within = all_within && within;
    if (d > max_distance) max_distance = d;

    Value rec = Value::Object();
    rec.set("index", Value(static_cast<double>(i)));
    rec.set("x", Value(p.x));
    rec.set("y", Value(p.y));
    rec.set("distance", Value(d));
    rec.set("nearest_segment_index",
            nearest < 0 ? Value() : Value(static_cast<double>(nearest)));
    rec.set("within_bound", Value(within));
    per_point.push_back(std::move(rec));
  }

  // ---- 组装响应 ----
  Value simplified = Value::Array();
  for (std::size_t k = 0; k < sr.kept.size(); ++k) {
    Value pt = Value::Object();
    pt.set("x", Value(sr.kept[k].x));
    pt.set("y", Value(sr.kept[k].y));
    pt.set("source_index",
           Value(static_cast<double>(sr.kept_indices[k])));
    simplified.push_back(std::move(pt));
  }

  Value removed = Value::Array();
  for (std::size_t idx : removed_indices) {
    removed.push_back(Value(static_cast<double>(idx)));
  }

  Value verification = Value::Object();
  verification.set("metric",
                   Value("point_to_polyline_euclidean_min_point_segment"));
  // 只声称单侧界：dist(每个原始点 -> 简化折线) <= epsilon。
  // 不声称双向 Hausdorff 界。
  verification.set("bound_semantics", Value("one_sided_original_to_simplified"));
  verification.set("bound", Value(epsilon));
  verification.set("numerical_tolerance", Value(tolerance));
  verification.set("max_distance", Value(max_distance));
  verification.set("within_bound", Value(all_within));
  verification.set("points", per_point);

  Value root = Value::Object();
  root.set("status", Value("ok"));
  root.set("coordinate_system",
           Value("planar_cartesian_2d_unitless_double"));
  root.set("epsilon", Value(epsilon));
  root.set("input_point_count", Value(static_cast<double>(points.size())));
  root.set("deduplicated_point_count",
           Value(static_cast<double>(dedup.size())));
  root.set("removed_duplicate_point_indices", removed);
  root.set("simplified", simplified);
  root.set("simplified_count",
           Value(static_cast<double>(sr.kept.size())));
  root.set("verification", verification);

  std::cout << cps::json::Dump(root) << "\n";
  return 0;
}
