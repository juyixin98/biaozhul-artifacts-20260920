// Unit tests for the frame forest, interpolation and validation.
// Numerical results are checked against hand-derived analytical values.
#include "../src/frame_tree.hpp"
#include "../src/tf_math.hpp"
#include "../src/crypto.hpp"

#include <cmath>
#include <iostream>
#include <string>

namespace {

int g_failures = 0;
int g_checks = 0;

void report(bool ok, const std::string& name, const std::string& detail) {
  ++g_checks;
  if (!ok) {
    ++g_failures;
    std::cout << "[FAIL] " << name << "  " << detail << "\n";
  } else {
    std::cout << "[ok]   " << name << "\n";
  }
}

bool approx(double a, double b, double tol = 1e-9) {
  return std::fabs(a - b) <= tol;
}

bool vecApprox(const tf::Vector3& a, const tf::Vector3& b,
               double tol = 1e-9) {
  return (a - b).norm() <= tol;
}

bool quatApprox(const tf::Quaternion& a, const tf::Quaternion& b,
                double tol = 1e-9) {
  // quaternions are equivalent up to sign
  double d = std::fabs(a.normalized().dot(b.normalized()));
  return std::fabs(d - 1.0) <= tol;
}

bool matApprox(const tf::Matrix3& a, const tf::Matrix3& b,
               double tol = 1e-9) {
  return (a - b).cwiseAbs().maxCoeff() <= tol;
}

#define EXPECT_THROW_CODE(expr, ecode, name)                                 \
  do {                                                                      \
    bool caught = false;                                                    \
    try {                                                                   \
      expr;                                                                 \
    } catch (const tf::TfError& e) {                                        \
      caught = (e.code == ecode);                                           \
      if (!caught)                                                          \
        report(false, name,                                                 \
               "wrong code: " + std::string(tf::errorCodeName(e.code)) +   \
                   " " + e.what());                                         \
    }                                                                       \
    if (caught) report(true, name, "");                                     \
  } while (0)

const double PI = 3.14159265358979323846;

tf::Transform makeXform(double tx, double ty, double tz,
                        const tf::Quaternion& q) {
  tf::Transform x;
  x.t = tf::Vector3(tx, ty, tz);
  x.q = q;
  return x;
}

void testInverseAnalytic() {
  // Static edge world->a: t=(1,0,0), q=Rz(90).
  // Analytic inverse (a->world): R^T=Rz(-90), t'=-Rz(-90)*(1,0,0)=(0,1,0).
  tf::FrameTree tree;
  tree.addStatic("world", "a",
                 makeXform(1, 0, 0, tf::Quaternion(tf::AngleAxisd(
                                        PI / 2, tf::Vector3::UnitZ()))));
  auto r = tree.query("a", "world", 0);
  tf::Vector3 expect_t(0, 1, 0);
  tf::Quaternion expect_q(tf::AngleAxisd(-PI / 2, tf::Vector3::UnitZ()));
  bool ok = vecApprox(r.xform.t, expect_t) && quatApprox(r.xform.q, expect_q);
  report(ok, "inverse transform vs analytic (t=(0,1,0), Rz-90)", "");

  // Round trip with a non-trivial rotation: T * T^-1 = I and point back.
  tf::Quaternion qa(tf::AngleAxisd(0.6435, tf::Vector3(1, 2, 2).normalized()));
  tf::Transform x = makeXform(1.5, -2.0, 0.7, qa);
  tf::Transform id = tf::compose(x, tf::inverse(x));
  bool id_ok = vecApprox(id.t, tf::Vector3::Zero(), 1e-12) &&
               matApprox(id.q.toRotationMatrix(), tf::Matrix3::Identity(),
                         1e-12);
  report(id_ok, "T * inverse(T) == identity", "");
}

void testChainAnalytic() {
  // world->a: t=(1,0,0), q=Rz90. a->b: t=(0,2,0), q=Rx90.
  // T_b_w = T2*T1: R=Rx90*Rz90, t=t2+Rx90*t1; Rx90*(1,0,0)=(1,0,0),
  // so t=(1,2,0).
  // R analytic: [[0,-1,0],[0,0,-1],[1,0,0]]
  tf::FrameTree tree;
  tree.addStatic("world", "a",
                 makeXform(1, 0, 0, tf::Quaternion(tf::AngleAxisd(
                                        PI / 2, tf::Vector3::UnitZ()))));
  tree.addStatic("a", "b",
                 makeXform(0, 2, 0, tf::Quaternion(tf::AngleAxisd(
                                        PI / 2, tf::Vector3::UnitX()))));
  auto r = tree.query("world", "b", 0);
  tf::Matrix3 expect_R;
  expect_R << 0, -1, 0, 0, 0, -1, 1, 0, 0;
  bool ok = vecApprox(r.xform.t, tf::Vector3(1, 2, 0)) &&
            matApprox(r.xform.q.toRotationMatrix(), expect_R);
  report(ok, "chain Rx90*(Rz90) analytic t=(1,2,0)", "");

  // b->world is the exact inverse.
  // R^T=[[0,0,1],[-1,0,0],[0,-1,0]]; R^T*(1,2,0)=(0,-1,-2); neg=(0,1,2).
  auto rb = tree.query("b", "world", 0);
  bool inv_ok = vecApprox(rb.xform.t, tf::Vector3(0, 1, 2), 1e-9) &&
                matApprox(rb.xform.q.toRotationMatrix(), expect_R.transpose());
  report(inv_ok, "chain inverse analytic t=(0,1,2)", "");
}

void testDynamicInterpolation() {
  tf::FrameTree tree;
  std::vector<tf::Sample> s;
  tf::Sample s0;
  s0.stamp_us = 0;
  s0.xform = makeXform(0, 0, 0, tf::Quaternion::Identity());
  tf::Sample s1;
  s1.stamp_us = 10;
  s1.xform = makeXform(10, 0, 0,
                       tf::Quaternion(tf::AngleAxisd(PI / 2,
                                                     tf::Vector3::UnitZ())));
  s = {s0, s1};
  tree.addDynamicSamples("world", "a", s);

  auto mid = tree.query("world", "a", 5);
  bool ok = vecApprox(mid.xform.t, tf::Vector3(5, 0, 0)) &&
            quatApprox(mid.xform.q,
                       tf::Quaternion(
                           tf::AngleAxisd(PI / 4, tf::Vector3::UnitZ())));
  report(ok, "dynamic midpoint: t=(5,0,0), Rz45", "");
  bool trace_ok = mid.trace.size() == 1 && mid.trace[0].interpolated &&
                  approx(mid.trace[0].alpha, 0.5) &&
                  mid.trace[0].sample_a == 1 && mid.trace[0].sample_b == 2 &&
                  mid.time_error_us == 0;
  report(trace_ok, "midpoint trace: bracket ids, alpha=0.5, time_error=0", "");

  auto at0 = tree.query("world", "a", 0);
  auto at10 = tree.query("world", "a", 10);
  bool ends_ok = quatApprox(at0.xform.q, tf::Quaternion::Identity()) &&
                 quatApprox(at10.xform.q,
                            tf::Quaternion(
                                tf::AngleAxisd(PI / 2, tf::Vector3::UnitZ()))) &&
                 !at0.trace[0].interpolated;
  report(ends_ok, "endpoint samples exact (no interpolation flag)", "");

  EXPECT_THROW_CODE(tree.query("world", "a", -1), tf::ErrorCode::kOutOfRange,
                    "no backward extrapolation");
  EXPECT_THROW_CODE(tree.query("world", "a", 11), tf::ErrorCode::kOutOfRange,
                    "no forward extrapolation");
}

void testNear180Slerp() {
  tf::Vector3 axis(1, 1, 0);
  axis.normalize();
  tf::Quaternion q0 = tf::Quaternion::Identity();

  // 179.999 degrees: nearly antipodal in quaternion space.
  double big = 179.999 * PI / 180.0;
  tf::Quaternion q1(tf::AngleAxisd(big, axis));
  tf::Quaternion qm = tf::slerpShortest(q0, q1, 0.5);
  tf::Quaternion expect(tf::AngleAxisd(big / 2, axis));
  bool ok = quatApprox(qm, expect, 1e-9) &&
            std::isfinite(qm.w()) && std::isfinite(qm.x());
  report(ok, "slerp near 180deg midpoint analytic", "");

  // Negated q1 encodes the same rotation but the other arc; shortest-arc
  // SLERP must still follow the short way.
  tf::Quaternion q1neg;
  q1neg.coeffs() = -q1.coeffs();
  tf::Quaternion qm2 = tf::slerpShortest(q0, q1neg, 0.5);
  report(quatApprox(qm2, expect, 1e-9), "slerp handles antipodal sign flip",
         "");

  // Exactly 180 degrees: dot = 0, half is exactly 90.
  tf::Quaternion q180(tf::AngleAxisd(PI, axis));
  tf::Quaternion q90 = tf::slerpShortest(q0, q180, 0.5);
  report(quatApprox(q90, tf::Quaternion(tf::AngleAxisd(PI / 2, axis)), 1e-9),
         "slerp exact 180deg -> 90deg midpoint", "");
}

void testTreeValidation() {
  tf::FrameTree tree;
  tree.addStatic("a", "b", tf::Transform{});
  tree.addStatic("b", "c", tf::Transform{});

  EXPECT_THROW_CODE(tree.addStatic("c", "a", tf::Transform{}),
                    tf::ErrorCode::kCycleDetected, "cycle c->a rejected");
  EXPECT_THROW_CODE(tree.addStatic("a", "a", tf::Transform{}),
                    tf::ErrorCode::kCycleDetected, "self-loop rejected");
  EXPECT_THROW_CODE(tree.addStatic("x", "b", tf::Transform{}),
                    tf::ErrorCode::kMultiParentConflict,
                    "second parent of b rejected");
}

void testStaticDynamicConflict() {
  tf::FrameTree tree;
  tree.addStatic("a", "b", tf::Transform{});
  std::vector<tf::Sample> s;
  tf::Sample s0;
  s0.stamp_us = 0;
  s.push_back(s0);
  EXPECT_THROW_CODE(tree.addDynamicSamples("a", "b", s),
                    tf::ErrorCode::kMultiParentConflict,
                    "dynamic re-parent over static rejected");

  tf::FrameTree tree2;
  tree2.addDynamicSamples("a", "b", s);
  EXPECT_THROW_CODE(tree2.addStatic("a", "b", tf::Transform{}),
                    tf::ErrorCode::kMultiParentConflict,
                    "static re-parent over dynamic rejected");
}

void testQuaternionValidation() {
  tf::Quaternion q;
  auto chk1 = tf::validateQuaternion(1, 0, 0, 0, &q);
  report(chk1.ok, "identity quaternion accepted", chk1.error);

  auto chk2 = tf::validateQuaternion(0.707, 0.707, 0, 0, &q);  // |q|~0.99985
  report(chk2.ok, "near-unit quaternion accepted and normalized",
         chk2.error);
  report(approx(q.norm(), 1.0, 1e-15), "accepted quaternion renormalized", "");

  auto chk3 = tf::validateQuaternion(0.5, 0.5, 0, 0, &q);  // norm ~0.707
  report(!chk3.ok, "non-unit quaternion rejected", "");

  auto chk4 = tf::validateQuaternion(0, 0, 0, 0, &q);
  report(!chk4.ok, "zero quaternion rejected", "");

  auto chk5 = tf::validateQuaternion(std::nan(""), 0, 0, 0, &q);
  report(!chk5.ok, "NaN quaternion rejected", "");
}

void testMissingAndDisconnected() {
  tf::FrameTree tree;
  tree.addStatic("a", "b", tf::Transform{});
  tree.addStatic("c", "d", tf::Transform{});  // separate component
  EXPECT_THROW_CODE(tree.query("a", "zzz", 0),
                    tf::ErrorCode::kFrameNotFound, "unknown frame rejected");
  EXPECT_THROW_CODE(tree.query("b", "d", 0),
                    tf::ErrorCode::kDisconnected,
                    "frames in separate trees rejected");
}

void testDuplicateAndAsyncSamples() {
  tf::FrameTree tree;
  // Insert out of chronological order (asynchronous arrival).
  std::vector<tf::Sample> first;
  tf::Sample s10;
  s10.stamp_us = 10;
  s10.xform = makeXform(10, 0, 0, tf::Quaternion::Identity());
  first.push_back(s10);
  tree.addDynamicSamples("a", "b", first);

  std::vector<tf::Sample> second;
  tf::Sample s0;
  s0.stamp_us = 0;
  s0.xform = makeXform(0, 0, 0, tf::Quaternion::Identity());
  tf::Sample s5;
  s5.stamp_us = 5;
  s5.xform = makeXform(5, 0, 0, tf::Quaternion::Identity());
  second = {s0, s5};
  tree.addDynamicSamples("a", "b", second);

  auto r = tree.query("a", "b", 7);
  bool ok = vecApprox(r.xform.t, tf::Vector3(7, 0, 0)) &&
            r.trace[0].stamp_a_us == 5 && r.trace[0].stamp_b_us == 10;
  report(ok, "async (unordered) sample arrival interpolates correctly", "");

  std::vector<tf::Sample> dup;
  tf::Sample d;
  d.stamp_us = 5;
  dup.push_back(d);
  EXPECT_THROW_CODE(tree.addDynamicSamples("a", "b", dup),
                    tf::ErrorCode::kDuplicateTimestamp,
                    "duplicate timestamp rejected");

  std::vector<tf::Sample> dupBatch(2);
  dupBatch[0].stamp_us = 20;
  dupBatch[1].stamp_us = 20;
  EXPECT_THROW_CODE(tree.addDynamicSamples("a", "b", dupBatch),
                    tf::ErrorCode::kDuplicateTimestamp,
                    "in-batch duplicate timestamp rejected");

  // Failed insertion is transactional: 20 was not added.
  EXPECT_THROW_CODE(tree.query("a", "b", 20),
                    tf::ErrorCode::kOutOfRange,
                    "failed batch left tree unchanged");
}

void testLatestAsyncSpread() {
  // Two dynamic edges with samples at different times. Latest mode uses the
  // newest of each and reports the spread across the path.
  tf::FrameTree tree;
  std::vector<tf::Sample> sa;
  tf::Sample a1;
  a1.stamp_us = 1000;
  a1.xform = makeXform(1, 0, 0, tf::Quaternion::Identity());
  sa.push_back(a1);
  tree.addDynamicSamples("world", "a", sa);

  std::vector<tf::Sample> sb;
  tf::Sample b1;
  b1.stamp_us = 3000;
  b1.xform = makeXform(0, 1, 0, tf::Quaternion::Identity());
  sb.push_back(b1);
  tree.addDynamicSamples("a", "b", sb);

  auto r = tree.queryLatest("world", "b");
  bool ok = vecApprox(r.xform.t, tf::Vector3(1, 1, 0)) &&
            r.time_error_us == 2000 && r.trace.size() == 2;
  report(ok, "latest query: composed newest samples, spread=2000us", "");

  // A timed query inside the joint window still fails if ANY edge lacks
  // coverage: t=2000 is fine for a (only one sample at 1000 -> out of range).
  EXPECT_THROW_CODE(tree.query("world", "b", 2000),
                    tf::ErrorCode::kOutOfRange,
                    "timed query fails when one edge lacks coverage");

  // Empty dynamic edge query.
  tf::FrameTree empty;
  EXPECT_THROW_CODE(empty.queryLatest("x", "y"),
                    tf::ErrorCode::kFrameNotFound, "unknown in latest");
}

void testStaticEdgeRestate() {
  tf::FrameTree tree;
  tree.addStatic("a", "b", makeXform(1, 0, 0, tf::Quaternion::Identity()));
  // Re-stating the SAME static edge updates it, no conflict.
  tree.addStatic("a", "b", makeXform(2, 0, 0, tf::Quaternion::Identity()));
  auto r = tree.query("a", "b", 0);
  report(vecApprox(r.xform.t, tf::Vector3(2, 0, 0)),
         "restating identical static edge updates value", "");
}

void testHmacRfcVectors() {
  // RFC 4231 test case 1 and test case 2 (truncated to known hex).
  std::string key1(20, '\x0b');
  std::string got1 = tf::hmacSha256Hex(key1, "Hi There");
  const char* exp1 =
      "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7";
  report(got1 == exp1, "HMAC-SHA256 RFC4231 case 1 known-answer",
         got1 + " != " + exp1);

  std::string got2 = tf::hmacSha256Hex("Jefe", "what do ya want for nothing?");
  const char* exp2 =
      "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843";
  report(got2 == exp2, "HMAC-SHA256 RFC4231 case 2 known-answer",
         got2 + " != " + exp2);
}

}  // namespace

int main() {
  testInverseAnalytic();
  testChainAnalytic();
  testDynamicInterpolation();
  testNear180Slerp();
  testTreeValidation();
  testStaticDynamicConflict();
  testQuaternionValidation();
  testMissingAndDisconnected();
  testDuplicateAndAsyncSamples();
  testLatestAsyncSpread();
  testStaticEdgeRestate();
  testHmacRfcVectors();

  std::cout << "\n" << (g_checks - g_failures) << "/" << g_checks
            << " checks passed\n";
  return g_failures == 0 ? 0 : 1;
}
