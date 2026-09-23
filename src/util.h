#pragma once
// SE(2) group helpers + small general utilities.
#include <array>
#include <cmath>
#include <string>
#include <vector>

#include "types.h"

namespace pgo {

constexpr double kPi = 3.141592653589793238462643383279502884;

// Normalize an angle to (-pi, pi] — handles angles crossing +/- pi.
inline double NormalizeAngle(double a) {
  if (std::isnan(a)) return a;
  double r = std::fmod(a + kPi, 2.0 * kPi);
  if (r < 0.0) r += 2.0 * kPi;
  return r - kPi;
}

// Pose composition: a (*this) then b, in the fixed world frame convention.
// out = a \oplus b where
//   R(a) = [cos ta, -sin ta; sin ta, cos ta]
//   out.x = a.x + R(a) * b.xy ; out.t = wrap(a.t + b.t)
Pose Compose(const Pose& a, const Pose& b);

// Group inverse: pose such that Compose(a, Inverse(a)) == identity.
Pose Inverse(const Pose& a);

// Relative pose error in the local frame of `from`, with heading wrapped:
//   e_xy  = R(tf)^-1 ( t_from R(from) z - t_to )
//   e_t   = wrap(z.t - (to.t - from.t))
// Equivalent to Inverse(measurement) * (inverse(from) * to).
std::array<double, 3> EdgeResidual(const Pose& from, const Pose& to,
                                   const Pose& measurement);

bool Approx(double a, double b, double tol = 1e-9);

// ISO-8601 UTC timestamp, e.g. 2026-09-23T08:14:02Z.
std::string UtcTimestamp();

// 16 random hex chars.
std::string RandomHex8();

// Globally-unique run identifier: timestamp + pid + monotonic counter + hex.
std::string NewRunId();

// Read/write a whole file (binary).
bool ReadFile(const std::string& path, std::string* out, std::string* err);
bool WriteFileAtomic(const std::string& path, const std::string& content,
                     std::string* err);

// Signal handling: Solve checks IsCancelRequested() between iterations.
void InstallSignalHandlers();
bool IsCancelRequested();
void RequestCancel();

// Sleep used by the --sleep-before-ms test hook.
void SleepMs(int ms);

}  // namespace pgo
