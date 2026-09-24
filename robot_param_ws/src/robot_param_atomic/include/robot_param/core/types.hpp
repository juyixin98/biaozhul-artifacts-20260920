// SPDX-License-Identifier: Apache-2.0
//
// Core value types for atomic robot parameter configuration.
// Everything here is ROS-independent so the domain logic can be unit-tested
// without a ROS 2 executor.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace robot_param {

// Immutable configuration revision.
//
// Field semantics (the cross-field constraint lives in validation.cpp):
//   sample_rate_hz    sampling rate [Hz],                    must be > 0
//   buffer_length     ring-buffer length [samples],          must be > 0
//   allowed_latency_ms tolerable latency budget [ms],         must be > 0
//
// Physical meaning of the invariant: the buffer must cover at least twice the
// allowed latency window.
//   buffer_length / sample_rate_hz * 1000.0 >= 2.0 * allowed_latency_ms
struct ConfigSnapshot {
  std::uint64_t version = 0;           // 0 = built-in default; never persisted
  double sample_rate_hz = 100.0;
  std::uint32_t buffer_length = 1000;  // samples
  std::uint64_t allowed_latency_ms = 50;
  std::int64_t committed_at_ms = 0;    // Unix epoch milliseconds (0 for v0)

  // Duration the buffer covers, in milliseconds (buffer_length / rate * 1000).
  // Returns 0.0 for non-positive sample rate.
  double buffer_covers_ms() const;

  bool operator==(const ConfigSnapshot& other) const;
  bool operator!=(const ConfigSnapshot& other) const { return !(*this == other); }
};

// Partial batch update: only fields whose set_* flag is present are touched.
struct ConfigPatch {
  bool set_sample_rate = false;
  double sample_rate_hz = 0.0;
  bool set_buffer_length = false;
  std::uint32_t buffer_length = 0;
  bool set_allowed_latency = false;
  std::uint64_t allowed_latency_ms = 0;
};

// Result of validating a field value in isolation.
struct FieldCheck {
  bool ok = true;
  std::string error;  // empty when ok
};

// Applies a patch to a snapshot (no validation; callers validate afterwards).
ConfigSnapshot apply_patch(const ConfigSnapshot& base, const ConfigPatch& patch);

// Per-field validation. Each field passes on its own; a triplet may still fail
// the cross-field invariant (this is exactly what the "single field legal but
// combination illegal" test exercises).
FieldCheck check_sample_rate(double hz);
FieldCheck check_buffer_length(std::uint32_t samples);
FieldCheck check_allowed_latency(std::uint64_t ms);

// Full validation: per-field ranges first, then the cross-field invariant.
// Returns {true, ""} for a legal snapshot.
FieldCheck check_snapshot(const ConfigSnapshot& s);

}  // namespace robot_param
