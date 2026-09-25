// test_interp.cpp — 轨迹时间插值的自动化单元测试(自包含,无外部框架)。
//
// 覆盖验收要求:
//   - 180° 附近、小角度、相反符号四元数的 SLERP 正确性
//   - 输出单位范数
//   - 端点一致性
//   - 断档/外推拒绝、重复时间戳拒绝
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>

#include "traj/json.hpp"
#include "traj/trajectory.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& name) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::cerr << "FAIL: " << name << "\n";
  }
}

bool near(double a, double b, double tol) { return std::fabs(a - b) <= tol; }

bool quatNear(const traj::Quat& a, const traj::Quat& b, double tol) {
  return near(a.w, b.w, tol) && near(a.x, b.x, tol) && near(a.y, b.y, tol) &&
         near(a.z, b.z, tol);
}

// 旋转意义上的等价:q == ±b
bool quatSameRotation(const traj::Quat& a, const traj::Quat& b, double tol) {
  return quatNear(a, b, tol) ||
         quatNear(a, {-b.w, -b.x, -b.y, -b.z}, tol);
}

traj::Quat axisAngle(double ax, double ay, double az, double deg) {
  const double half = deg * traj::kPi / 360.0;
  const double s = std::sin(half);
  return {std::cos(half), ax * s, ay * s, az * s};
}

traj::Pose makePose(double t, traj::Vec3 p, traj::Quat q) {
  traj::Pose pose;
  pose.t = t;
  pose.position = p;
  pose.orientation = q;
  return pose;
}

// 1. 位置线性插值
void testLinearPosition() {
  traj::Trajectory tr({makePose(0.0, {0, 0, 0}, {}), makePose(2.0, {4, -2, 1}, {})});
  const auto r = tr.at(1.0);
  check(r.ok, "linear: query ok");
  check(near(r.pose.position.x, 2.0, 1e-15) && near(r.pose.position.y, -1.0, 1e-15) &&
            near(r.pose.position.z, 0.5, 1e-15),
        "linear: midpoint position");
}

// 2. SLERP:0° → 90° 绕 z,中点应为 45°
void testSlerpQuarter() {
  traj::Trajectory tr({makePose(0.0, {}, axisAngle(0, 0, 1, 0.0)),
                       makePose(1.0, {}, axisAngle(0, 0, 1, 90.0))});
  const auto r = tr.at(0.5);
  check(r.ok, "slerp90: query ok");
  check(quatSameRotation(r.pose.orientation, axisAngle(0, 0, 1, 45.0), 1e-12),
        "slerp90: midpoint is 45 deg");
  check(near(traj::norm(r.pose.orientation), 1.0, 1e-12), "slerp90: unit norm");
}

// 3. 小角度(0.001°):nlerp 退化路径,仍需单位范数且角度连续
void testSmallAngle() {
  traj::Trajectory tr({makePose(0.0, {}, axisAngle(0, 1, 0, 0.0)),
                       makePose(1.0, {}, axisAngle(0, 1, 0, 0.001))});
  double prev = -1.0;
  bool monotone = true;
  bool unitNorm = true;
  for (int i = 0; i <= 100; ++i) {
    const auto r = tr.at(i / 100.0);
    if (!r.ok) { unitNorm = false; break; }
    if (!near(traj::norm(r.pose.orientation), 1.0, 1e-12)) unitNorm = false;
    const double ang = traj::angleDeg(axisAngle(0, 1, 0, 0.0), r.pose.orientation);
    if (ang < prev - 1e-9) monotone = false;
    prev = ang;
  }
  check(unitNorm, "small-angle: unit norm along path");
  check(monotone, "small-angle: angle increases monotonically");
  check(near(prev, 0.001, 1e-9), "small-angle: reaches final angle");
}

// 4. 相反符号四元数:q 与 -q 是同一旋转,插值必须恒定,不得翻转
void testOppositeSign() {
  const traj::Quat q = axisAngle(0.0, 0.0, 1.0, 37.0);
  traj::Trajectory tr({makePose(0.0, {}, q), makePose(1.0, {}, {-q.w, -q.x, -q.y, -q.z})});
  bool constant = true;
  bool unitNorm = true;
  for (int i = 0; i <= 50; ++i) {
    const auto r = tr.at(i / 50.0);
    if (!r.ok || !quatSameRotation(r.pose.orientation, q, 1e-9)) constant = false;
    if (r.ok && !near(traj::norm(r.pose.orientation), 1.0, 1e-12)) unitNorm = false;
  }
  check(constant, "opposite-sign: rotation stays constant (sign equivalence)");
  check(unitNorm, "opposite-sign: unit norm");
}

// 5. 恰好 180°:identity → (0,0,0,1),中点应为 90° 绕 z
void testExactly180() {
  traj::Trajectory tr({makePose(0.0, {}, {1, 0, 0, 0}), makePose(1.0, {}, {0, 0, 0, 1})});
  const auto r = tr.at(0.5);
  check(r.ok, "180: query ok");
  check(quatSameRotation(r.pose.orientation, axisAngle(0, 0, 1, 90.0), 1e-12),
        "180: midpoint is 90 deg about z");
  check(near(traj::norm(r.pose.orientation), 1.0, 1e-12), "180: unit norm");
}

// 6. 180° 附近(179.9° 与 180.1° 等效为 179.9° 短弧):单位范数 + 总角度正确
void testNear180() {
  for (const double deg : {179.9, 179.999999}) {
    traj::Trajectory tr({makePose(0.0, {}, axisAngle(1, 0, 0, 0.0)),
                         makePose(1.0, {}, axisAngle(1, 0, 0, deg))});
    bool unitNorm = true;
    for (int i = 0; i <= 200; ++i) {
      const auto r = tr.at(i / 200.0);
      if (!r.ok || !near(traj::norm(r.pose.orientation), 1.0, 1e-12)) unitNorm = false;
    }
    check(unitNorm, "near-180 (" + std::to_string(deg) + "): unit norm along path");
    const auto mid = tr.at(0.5);
    check(near(traj::angleDeg(axisAngle(1, 0, 0, 0.0), mid.pose.orientation), deg / 2, 1e-6),
          "near-180 (" + std::to_string(deg) + "): midpoint angle is half");
  }
}

// 7. 端点一致性:查询 t_first / t_last 精确返回端点位姿
void testEndpoints() {
  const traj::Quat q0 = axisAngle(0, 0, 1, 10.0);
  const traj::Quat q1 = axisAngle(1, 1, 0, 80.0);
  traj::Trajectory tr({makePose(1.5, {1, 2, 3}, q0), makePose(2.5, {4, 5, 6}, q1)});
  const auto a = tr.at(1.5);
  const auto b = tr.at(2.5);
  check(a.ok && quatNear(a.pose.orientation, traj::normalize(q0), 0.0) &&
            near(a.pose.position.x, 1.0, 0.0),
        "endpoints: first pose exact");
  check(b.ok && quatNear(b.pose.orientation, traj::normalize(q1), 0.0) &&
            near(b.pose.position.z, 6.0, 0.0),
        "endpoints: last pose exact");
}

// 8. 多段轨迹:跨段扫描单位范数,并验证中间关键点处取值
void testMultiSegment() {
  traj::Trajectory tr({makePose(0.0, {0, 0, 0}, axisAngle(0, 0, 1, 0.0)),
                       makePose(1.0, {1, 0, 0}, axisAngle(0, 0, 1, 90.0)),
                       makePose(3.0, {1, 2, 0}, axisAngle(0, 0, 1, 90.0)),
                       makePose(4.0, {1, 2, 2}, axisAngle(0, 1, 0, 90.0))});
  bool unitNorm = true;
  for (int i = 0; i <= 400; ++i) {
    const auto r = tr.at(i / 100.0);
    if (!r.ok || !near(traj::norm(r.pose.orientation), 1.0, 1e-12)) unitNorm = false;
  }
  check(unitNorm, "multi-segment: unit norm across segments");
  const auto mid = tr.at(2.0);  // 第二段中点,姿态恒定段
  check(mid.ok && quatSameRotation(mid.pose.orientation, axisAngle(0, 0, 1, 90.0), 1e-12) &&
            near(mid.pose.position.y, 1.0, 1e-15),
        "multi-segment: constant-rotation segment interpolates position only");
}

// 9. 重复时间戳:构造必须拒绝
void testDuplicateTimestamps() {
  bool threw = false;
  try {
    traj::Trajectory tr({makePose(1.0, {}, {}), makePose(1.0, {}, {})});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "duplicate timestamps rejected");
  bool threwOrder = false;
  try {
    traj::Trajectory tr({makePose(2.0, {}, {}), makePose(1.0, {}, {})});
  } catch (const std::invalid_argument&) {
    threwOrder = true;
  }
  check(threwOrder, "out-of-order timestamps rejected");
}

// 10. 断档/外推拒绝:范围外查询必须失败
void testOutOfRange() {
  traj::Trajectory tr({makePose(1.0, {}, {}), makePose(2.0, {}, {})});
  check(!tr.at(0.999999).ok && tr.at(0.999999).error == "out_of_range",
        "out-of-range: before first rejected");
  check(!tr.at(2.000001).ok && tr.at(2.000001).error == "out_of_range",
        "out-of-range: after last rejected");
  check(!tr.at(std::nan("")).ok, "out-of-range: NaN query rejected");
}

// 11. 单点轨迹:仅 t == t0 可查询
void testSinglePose() {
  traj::Trajectory tr({makePose(5.0, {1, 1, 1}, axisAngle(0, 0, 1, 30.0))});
  check(tr.at(5.0).ok, "single-pose: exact query ok");
  check(!tr.at(5.0 + 1e-9).ok, "single-pose: any offset rejected");
}

// 12. 非单位四元数输入:构造时归一化,结果与单位输入一致
void testNonUnitInput() {
  const traj::Quat q = axisAngle(0, 1, 0, 60.0);
  const traj::Quat scaled = {3 * q.w, 3 * q.x, 3 * q.y, 3 * q.z};
  traj::Trajectory tr({makePose(0.0, {}, scaled), makePose(1.0, {}, axisAngle(0, 1, 0, 120.0))});
  const auto r = tr.at(0.5);
  check(r.ok && quatSameRotation(r.pose.orientation, axisAngle(0, 1, 0, 90.0), 1e-12),
        "non-unit input normalized");
}

// 13. 零范数四元数:构造必须拒绝
void testZeroNormQuaternion() {
  bool threw = false;
  try {
    traj::Trajectory tr({makePose(0.0, {}, {0, 0, 0, 0}), makePose(1.0, {}, {})});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "zero-norm quaternion rejected");
}

// 14. 符号等价不引入长弧:q 到 -(q 旋转 1°) 的插值应走 1° 短弧而非 359°
void testShortestArcWithSignFlip() {
  const traj::Quat q0 = axisAngle(0, 0, 1, 0.0);
  const traj::Quat q1raw = axisAngle(0, 0, 1, 1.0);
  const traj::Quat q1 = {-q1raw.w, -q1raw.x, -q1raw.y, -q1raw.z};  // 同一旋转,反号
  traj::Trajectory tr({makePose(0.0, {}, q0), makePose(1.0, {}, q1)});
  const auto mid = tr.at(0.5);
  check(mid.ok && near(traj::angleDeg(q0, mid.pose.orientation), 0.5, 1e-9),
        "sign-flipped 1 deg: midpoint is 0.5 deg (short arc)");
}

// 15. JSON:解析 + 序列化往返,double 精度保持
void testJsonRoundTrip() {
  const std::string text =
      R"({"a":[1,2.5,-3.25e2],"b":{"c":"x\ny","d":true,"e":null},"pi":3.141592653589793})";
  const auto v = traj::json::parse(text);
  check(v.find("a") && v.find("a")->arr.size() == 3, "json: array parsed");
  check(v.find("pi") && near(v.find("pi")->number, 3.141592653589793, 0.0),
        "json: double round-trips exactly");
  const auto v2 = traj::json::parse(traj::json::dump(v));
  check(traj::json::dump(v2) == traj::json::dump(v), "json: dump/parse stable");
  bool threw = false;
  try {
    traj::json::parse("{\"a\": 1,}");
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "json: trailing comma rejected");
}

}  // namespace

int main() {
  testLinearPosition();
  testSlerpQuarter();
  testSmallAngle();
  testOppositeSign();
  testExactly180();
  testNear180();
  testEndpoints();
  testMultiSegment();
  testDuplicateTimestamps();
  testOutOfRange();
  testSinglePose();
  testNonUnitInput();
  testZeroNormQuaternion();
  testShortestArcWithSignFlip();
  testJsonRoundTrip();

  std::cout << (g_checks - g_failures) << "/" << g_checks << " checks passed\n";
  if (g_failures > 0) {
    std::cout << g_failures << " FAILURES\n";
    return 1;
  }
  std::cout << "ALL TESTS PASSED\n";
  return 0;
}
