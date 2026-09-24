// SPDX-License-Identifier: Apache-2.0
#include <gtest/gtest.h>

#include <cmath>
#include <limits>

#include "robot_param/core/types.hpp"

using namespace robot_param;

namespace {

ConfigSnapshot make(double rate, std::uint32_t buffer, std::uint64_t latency) {
  ConfigSnapshot s;
  s.sample_rate_hz = rate;
  s.buffer_length = buffer;
  s.allowed_latency_ms = latency;
  return s;
}

}  // namespace

TEST(Validation, DefaultsAreLegal) {
  EXPECT_TRUE(check_snapshot(ConfigSnapshot{}).ok);
  // Defaults: 1000 samples / 100 Hz = 1000 ms covered >= 2*50 ms.
  EXPECT_DOUBLE_EQ(ConfigSnapshot{}.buffer_covers_ms(), 10000.0);
}

TEST(Validation, PerFieldRanges) {
  EXPECT_FALSE(check_sample_rate(0.0).ok);
  EXPECT_FALSE(check_sample_rate(-1.0).ok);
  EXPECT_FALSE(check_sample_rate(std::nan("")).ok);
  EXPECT_FALSE(check_sample_rate(std::numeric_limits<double>::infinity()).ok);
  EXPECT_TRUE(check_sample_rate(1.0).ok);
  EXPECT_TRUE(check_sample_rate(999999.0).ok);

  EXPECT_FALSE(check_buffer_length(0).ok);
  EXPECT_TRUE(check_buffer_length(1).ok);
  EXPECT_TRUE(check_buffer_length(100000000u).ok);

  EXPECT_FALSE(check_allowed_latency(0).ok);
  EXPECT_TRUE(check_allowed_latency(1).ok);
  EXPECT_FALSE(check_allowed_latency(86400001ull).ok);
}

// THE key case: every field is legal in isolation but the combination breaks
// the "buffer covers >= 2x allowed latency" invariant.
TEST(Validation, SingleFieldsLegalButCombinationIllegal) {
  // rate=100 Hz (legal), buffer=100 samples (legal) -> covers 1000 ms
  // latency=1000 ms (legal in isolation) -> requires 2000 ms coverage.
  ConfigSnapshot s = make(100.0, 100, 1000);
  EXPECT_TRUE(check_sample_rate(s.sample_rate_hz).ok);
  EXPECT_TRUE(check_buffer_length(s.buffer_length).ok);
  EXPECT_TRUE(check_allowed_latency(s.allowed_latency_ms).ok);
  FieldCheck c = check_snapshot(s);
  ASSERT_FALSE(c.ok);
  EXPECT_NE(c.error.find("cross-field constraint"), std::string::npos);
  EXPECT_NE(c.error.find("1000.000 ms"), std::string::npos);
  EXPECT_NE(c.error.find("2000.000 ms"), std::string::npos);
}

TEST(Validation, BoundaryExactlyTwoTimesIsLegal) {
  // 200 samples / 100 Hz = 2000 ms covered; latency 1000 ms -> exactly 2x.
  EXPECT_TRUE(check_snapshot(make(100.0, 200, 1000)).ok);
  // One sample short: 199 samples -> 1990 ms < 2000 ms -> illegal.
  EXPECT_FALSE(check_snapshot(make(100.0, 199, 1000)).ok);
}

TEST(Validation, HighRateNeedsMoreSamples) {
  // 1000 Hz, latency 50 ms needs coverage of 100 ms = 100 samples.
  EXPECT_TRUE(check_snapshot(make(1000.0, 100, 50)).ok);
  EXPECT_FALSE(check_snapshot(make(1000.0, 99, 50)).ok);
}

TEST(Validation, PatchIsMergedNotReplaced) {
  ConfigSnapshot base = make(100.0, 1000, 50);
  ConfigPatch patch;
  patch.set_allowed_latency = true;
  patch.allowed_latency_ms = 400;  // 1000/100=10000ms covered, need 800: fine
  ConfigSnapshot merged = apply_patch(base, patch);
  EXPECT_EQ(merged.sample_rate_hz, 100.0);
  EXPECT_EQ(merged.buffer_length, 1000u);
  EXPECT_EQ(merged.allowed_latency_ms, 400u);
  EXPECT_TRUE(check_snapshot(merged).ok);

  ConfigPatch bad = patch;
  bad.allowed_latency_ms = 6000;  // need 12000 ms coverage: only 10000 ms
  EXPECT_FALSE(check_snapshot(apply_patch(base, bad)).ok);
}
