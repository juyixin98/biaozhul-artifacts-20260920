// SPDX-License-Identifier: Apache-2.0
#include "robot_param/core/types.hpp"

#include <cmath>
#include <limits>
#include <sstream>

namespace robot_param {

namespace {
// Tolerance for the cross-field comparison. The invariant involves converting
// samples <-> milliseconds, so we allow one microsecond of slack per second of
// latency budget; this never accepts a genuinely under-provisioned buffer but
// keeps boundary cases (e.g. exactly 2x) robust against rounding.
constexpr double kConstraintSlackMs = 1e-3;
}  // namespace

double ConfigSnapshot::buffer_covers_ms() const {
  if (!(sample_rate_hz > 0.0)) {
    return 0.0;
  }
  return static_cast<double>(buffer_length) / sample_rate_hz * 1000.0;
}

bool ConfigSnapshot::operator==(const ConfigSnapshot& o) const {
  return version == o.version && sample_rate_hz == o.sample_rate_hz &&
         buffer_length == o.buffer_length &&
         allowed_latency_ms == o.allowed_latency_ms &&
         committed_at_ms == o.committed_at_ms;
}

ConfigSnapshot apply_patch(const ConfigSnapshot& base, const ConfigPatch& p) {
  ConfigSnapshot out = base;
  if (p.set_sample_rate) {
    out.sample_rate_hz = p.sample_rate_hz;
  }
  if (p.set_buffer_length) {
    out.buffer_length = p.buffer_length;
  }
  if (p.set_allowed_latency) {
    out.allowed_latency_ms = p.allowed_latency_ms;
  }
  return out;
}

FieldCheck check_sample_rate(double hz) {
  if (std::isnan(hz) || std::isinf(hz)) {
    return {false, "sample_rate_hz must be a finite number"};
  }
  if (hz <= 0.0) {
    return {false, "sample_rate_hz must be > 0"};
  }
  if (hz > 1e6) {
    return {false, "sample_rate_hz must be <= 1000000"};
  }
  return {};
}

FieldCheck check_buffer_length(std::uint32_t samples) {
  if (samples == 0) {
    return {false, "buffer_length must be > 0 samples"};
  }
  if (samples > 100000000u) {
    return {false, "buffer_length must be <= 100000000 samples"};
  }
  return {};
}

FieldCheck check_allowed_latency(std::uint64_t ms) {
  if (ms == 0) {
    return {false, "allowed_latency_ms must be > 0"};
  }
  if (ms > 86400000ull) {
    return {false, "allowed_latency_ms must be <= 86400000 (24h)"};
  }
  return {};
}

FieldCheck check_snapshot(const ConfigSnapshot& s) {
  if (auto c = check_sample_rate(s.sample_rate_hz); !c.ok) {
    return c;
  }
  if (auto c = check_buffer_length(s.buffer_length); !c.ok) {
    return c;
  }
  if (auto c = check_allowed_latency(s.allowed_latency_ms); !c.ok) {
    return c;
  }

  const double covered_ms = s.buffer_covers_ms();
  const double required_ms = 2.0 * static_cast<double>(s.allowed_latency_ms);
  if (covered_ms + kConstraintSlackMs < required_ms) {
    std::ostringstream oss;
    oss.setf(std::ios::fixed);
    oss.precision(3);
    oss << "cross-field constraint violated: buffer covers " << covered_ms
        << " ms but must cover at least 2 * allowed_latency_ms = "
        << required_ms << " ms "
        << "(buffer_length=" << s.buffer_length
        << " samples at " << s.sample_rate_hz << " Hz)";
    return {false, oss.str()};
  }
  return {};
}

}  // namespace robot_param
