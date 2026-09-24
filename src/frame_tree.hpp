// The frame forest: static/dynamic edges keyed by child frame, cycle and
// multi-parent rejection, path composition over the tree, and time-windowed
// interpolation of dynamic edges (no extrapolation).
#pragma once

#include "tf_math.hpp"

#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace tf {

enum class ErrorCode {
  kOk,
  kFrameNotFound,
  kMultiParentConflict,
  kCycleDetected,
  kDisconnected,
  kInvalidQuaternion,
  kDuplicateTimestamp,
  kNoSamples,
  kOutOfRange,  // time outside sampled window: extrapolation is forbidden
};

const char* errorCodeName(ErrorCode c);

struct TfError : public std::runtime_error {
  ErrorCode code;
  TfError(ErrorCode c, const std::string& msg)
      : std::runtime_error(msg), code(c) {}
};

struct Sample {
  uint64_t id = 0;       // server-assigned, monotonic per edge
  int64_t stamp_us = 0;  // microseconds
  Transform xform;
};

struct Edge {
  std::string parent;
  std::string child;
  bool is_static = false;
  std::vector<Sample> samples;  // dynamic only; sorted by stamp
  Transform static_xform;       // static only
};

struct UsedSample {
  std::string frame;       // child frame identifying the edge
  bool is_static = false;
  bool interpolated = false;
  uint64_t sample_a = 0;   // bracket sample ids (dynamic)
  uint64_t sample_b = 0;
  int64_t stamp_a_us = 0;  // sample timestamps used
  int64_t stamp_b_us = 0;
  int64_t queried_us = 0;  // time the edge was evaluated at
  double alpha = 0.0;      // interpolation fraction in [0,1]
};

struct QueryResult {
  std::string from_frame;
  std::string to_frame;
  bool latest = false;
  Transform xform;                  // T_to_from: maps points in `from` into `to`
  std::vector<UsedSample> trace;    // one entry per edge on the path
  std::vector<std::string> path;    // frames walked, from -> to
  int64_t min_sample_time_us = 0;
  int64_t max_sample_time_us = 0;
  int64_t time_error_us = 0;        // 0 for timed queries; spread for latest
};

struct DumpNode {
  std::string frame;
  std::string parent;  // empty for roots
  bool is_static = false;
  size_t sample_count = 0;
  int64_t first_stamp_us = 0;
  int64_t last_stamp_us = 0;
};

class FrameTree {
 public:
  // Adds (or, for a repeated identical static edge, re-states) a static edge.
  // Throws TfError on multi-parent conflict or cycle.
  void addStatic(const std::string& parent, const std::string& child,
                 const Transform& x);

  // Creates a dynamic parent->child edge if absent, then inserts timestamped
  // samples. The whole batch is transactional: any invalid sample, duplicate
  // timestamp, conflict or cycle rejects everything.
  void addDynamicSamples(const std::string& parent,
                         const std::string& child,
                         const std::vector<Sample>& incoming);

  // Evaluate T_to_from at time t. Throws on missing/disconnected frames,
  // empty dynamic edges or out-of-window time.
  QueryResult query(const std::string& from, const std::string& to,
                    int64_t t_us) const;

  // Evaluate T_to_from using the newest sample of every dynamic edge on the
  // path. time_error_us reports the spread of the sample times actually used.
  QueryResult queryLatest(const std::string& from, const std::string& to) const;

  std::vector<std::string> frames() const;
  std::vector<DumpNode> dump() const;

 private:
  // Must be called with mutex_ held.
  void registerFrameLocked(const std::string& name);
  void checkParentLocked(const std::string& parent, const std::string& child,
                         bool is_static);
  bool createsCycleLocked(const std::string& parent,
                          const std::string& child) const;
  std::vector<std::string> findPathLocked(const std::string& from,
                                          const std::string& to) const;

  // Evaluate one edge's local transform in the direction of the path.
  Transform evalEdgeForTimeLocked(const Edge& e, int64_t t_us,
                                  UsedSample* used) const;
  Transform evalEdgeLatestLocked(const Edge& e, UsedSample* used) const;

  QueryResult queryLocked(const std::string& from, const std::string& to,
                          int64_t t_us, bool latest) const;

  mutable std::mutex mutex_;
  std::map<std::string, uint64_t> next_sample_id_;  // per child frame
  // Keyed by child frame: every frame has at most one parent (forest).
  std::map<std::string, Edge> edges_;
  std::map<std::string, std::vector<std::string>> children_;  // parent -> kids
};

}  // namespace tf
