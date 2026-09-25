// trajectory.hpp — 带时间戳的三维位姿序列与时间插值。
//
// 模型:
//   - 位姿 = 位置(Vec3,米)+ 姿态(单位四元数)。
//   - 时间戳为 double,单位秒,要求严格递增。
//   - 位置按时间线性插值,姿态按 SLERP 插值,同一参数 u。
//
// 策略(显式约定):
//   - 重复时间戳:输入非法,构造时拒绝(抛 invalid_argument)。
//   - 外推:不支持。查询时间超出 [t_first, t_last] 时查询失败
//     (返回 out_of_range 错误),不做任何外推。
//   - 断档:轨迹内部允许任意时间间隔,插值跨段进行;但查询落在
//     序列覆盖范围之外即拒绝。单点位姿仅在 t == t0 时可查询。
#pragma once

#include <algorithm>
#include <stdexcept>
#include <string>
#include <vector>

#include "traj/quat.hpp"

namespace traj {

struct Pose {
  double t = 0.0;  // 秒
  Vec3 position;
  Quat orientation;  // (w, x, y, z),构造时归一化
};

struct InterpResult {
  bool ok = false;
  std::string error;  // ok=false 时的机器可读错误码
  Pose pose;
};

class Trajectory {
 public:
  // 校验并构建。poses 至少 1 个;时间戳必须严格递增且有限;
  // 位置分量必须有限;四元数必须可归一化。
  explicit Trajectory(std::vector<Pose> poses) : poses_(std::move(poses)) {
    if (poses_.empty()) {
      throw std::invalid_argument("empty trajectory");
    }
    for (size_t i = 0; i < poses_.size(); ++i) {
      Pose& p = poses_[i];
      if (!std::isfinite(p.t)) {
        throw std::invalid_argument("non-finite timestamp at index " + std::to_string(i));
      }
      if (!std::isfinite(p.position.x) || !std::isfinite(p.position.y) ||
          !std::isfinite(p.position.z)) {
        throw std::invalid_argument("non-finite position at index " + std::to_string(i));
      }
      p.orientation = normalize(p.orientation);  // 零范数在此抛出
      if (i > 0 && !(p.t > poses_[i - 1].t)) {
        throw std::invalid_argument(
            "timestamps not strictly increasing (duplicate or out-of-order) at index " +
            std::to_string(i));
      }
    }
  }

  double tFirst() const { return poses_.front().t; }
  double tLast() const { return poses_.back().t; }
  size_t size() const { return poses_.size(); }

  // 在时刻 t 插值。范围外拒绝(不外推);端点精确命中时返回原始位姿。
  InterpResult at(double t) const {
    InterpResult r;
    if (!std::isfinite(t)) {
      r.error = "non_finite_query";
      return r;
    }
    if (t < tFirst() || t > tLast()) {
      r.error = "out_of_range";  // 断档/外推拒绝
      return r;
    }
    // 端点与单点:精确返回,避免浮点参数 u 的舍入。
    if (t == tFirst()) {
      r.ok = true;
      r.pose = poses_.front();
      return r;
    }
    if (t == tLast()) {
      r.ok = true;
      r.pose = poses_.back();
      return r;
    }
    // 二分查找所在段 [t_i, t_{i+1})。
    const auto it = std::upper_bound(
        poses_.begin(), poses_.end(), t,
        [](double v, const Pose& p) { return v < p.t; });
    const size_t i = static_cast<size_t>(std::distance(poses_.begin(), it)) - 1;
    const Pose& a = poses_[i];
    const Pose& b = poses_[i + 1];
    const double u = (t - a.t) / (b.t - a.t);  // 严格递增保证分母 > 0

    r.ok = true;
    r.pose.t = t;
    r.pose.position = lerp(a.position, b.position, u);
    r.pose.orientation = slerp(a.orientation, b.orientation, u);
    return r;
  }

 private:
  std::vector<Pose> poses_;
};

}  // namespace traj
