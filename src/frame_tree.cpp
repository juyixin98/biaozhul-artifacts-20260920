#include "frame_tree.hpp"

#include <algorithm>
#include <queue>
#include <set>
#include <unordered_map>

namespace tf {

const char* errorCodeName(ErrorCode c) {
  switch (c) {
    case ErrorCode::kOk: return "OK";
    case ErrorCode::kFrameNotFound: return "FRAME_NOT_FOUND";
    case ErrorCode::kMultiParentConflict: return "MULTI_PARENT_CONFLICT";
    case ErrorCode::kCycleDetected: return "CYCLE_DETECTED";
    case ErrorCode::kDisconnected: return "DISCONNECTED";
    case ErrorCode::kInvalidQuaternion: return "INVALID_QUATERNION";
    case ErrorCode::kDuplicateTimestamp: return "DUPLICATE_TIMESTAMP";
    case ErrorCode::kNoSamples: return "NO_SAMPLES";
    case ErrorCode::kOutOfRange: return "OUT_OF_RANGE";
  }
  return "UNKNOWN";
}

void FrameTree::registerFrameLocked(const std::string& name) {
  if (edges_.find(name) == edges_.end() &&
      children_.find(name) == children_.end()) {
    children_[name] = {};  // creates an empty (root so far) adjacency entry
  }
}

void FrameTree::checkParentLocked(const std::string& parent,
                                  const std::string& child, bool /*is_static*/) {
  auto it = edges_.find(child);
  if (it != edges_.end() && it->second.parent != parent) {
    throw TfError(
        ErrorCode::kMultiParentConflict,
        "frame '" + child + "' already has parent '" + it->second.parent +
            "'; refusing second parent '" + parent + "'");
  }
}

// Adding parent->child creates a cycle iff child is already an ancestor of
// parent (i.e. parent is reachable from child following edges downward).
bool FrameTree::createsCycleLocked(const std::string& parent,
                                   const std::string& child) const {
  std::queue<std::string> q;
  q.push(child);
  std::set<std::string> seen{child};
  while (!q.empty()) {
    std::string cur = q.front();
    q.pop();
    if (cur == parent) return true;
    auto adj = children_.find(cur);
    if (adj == children_.end()) continue;
    for (const auto& nxt : adj->second) {
      if (seen.insert(nxt).second) q.push(nxt);
    }
  }
  return false;
}

void FrameTree::addStatic(const std::string& parent,
                          const std::string& child, const Transform& x) {
  if (parent.empty() || child.empty())
    throw TfError(ErrorCode::kFrameNotFound, "frame name must not be empty");
  if (parent == child)
    throw TfError(ErrorCode::kCycleDetected,
                   "self-loop on frame '" + child + "' is a cycle");

  std::lock_guard<std::mutex> lock(mutex_);
  registerFrameLocked(parent);
  registerFrameLocked(child);
  checkParentLocked(parent, child, true);

  auto it = edges_.find(child);
  if (it == edges_.end()) {
    if (createsCycleLocked(parent, child))
      throw TfError(ErrorCode::kCycleDetected,
                     "adding edge '" + parent + "'->'" + child +
                         "' would create a cycle");
    Edge e;
    e.parent = parent;
    e.child = child;
    e.is_static = true;
    e.static_xform = x;
    edges_[child] = std::move(e);
    children_[parent].push_back(child);
  } else {
    if (!it->second.is_static)
      throw TfError(ErrorCode::kMultiParentConflict,
                     "frame '" + child + "' is already a dynamic edge");
    it->second.static_xform = x;  // restating same static edge: update value
  }
}

void FrameTree::addDynamicSamples(const std::string& parent,
                                  const std::string& child,
                                  const std::vector<Sample>& incoming) {
  if (parent.empty() || child.empty())
    throw TfError(ErrorCode::kFrameNotFound, "frame name must not be empty");
  if (parent == child)
    throw TfError(ErrorCode::kCycleDetected,
                   "self-loop on frame '" + child + "' is a cycle");
  if (incoming.empty())
    throw TfError(ErrorCode::kNoSamples,
                   "sample batch for '" + child + "' is empty");

  std::lock_guard<std::mutex> lock(mutex_);
  registerFrameLocked(parent);
  registerFrameLocked(child);
  checkParentLocked(parent, child, false);

  auto it = edges_.find(child);
  const bool existing = it != edges_.end();
  if (existing && it->second.is_static)
    throw TfError(ErrorCode::kMultiParentConflict,
                   "frame '" + child + "' is already a static edge");

  // Duplicate-timestamp validation against current samples and within batch.
  std::vector<int64_t> stamps;
  stamps.reserve(incoming.size());
  for (const auto& s : incoming) stamps.push_back(s.stamp_us);
  std::sort(stamps.begin(), stamps.end());
  for (size_t i = 1; i < stamps.size(); ++i) {
    if (stamps[i] == stamps[i - 1])
      throw TfError(ErrorCode::kDuplicateTimestamp,
                     "duplicate timestamp " +
                         std::to_string(stamps[i]) + "us in batch for '" +
                         child + "'");
  }
  if (existing) {
    const auto& cur = it->second.samples;
    size_t ci = 0;
    for (int64_t ns : stamps) {
      while (ci < cur.size() && cur[ci].stamp_us < ns) ++ci;
      if (ci < cur.size() && cur[ci].stamp_us == ns)
        throw TfError(ErrorCode::kDuplicateTimestamp,
                       "timestamp " + std::to_string(ns) +
                           "us already exists on edge '" + parent + "'->'" +
                           child + "'");
    }
  }

  if (!existing && createsCycleLocked(parent, child))
    throw TfError(ErrorCode::kCycleDetected,
                   "adding edge '" + parent + "'->'" + child +
                       "' would create a cycle");

  uint64_t& next_id = next_sample_id_[child];
  if (!existing) next_id = 0;

  std::vector<Sample> assigned;
  assigned.reserve(incoming.size());
  for (const auto& s : incoming) {
    Sample a = s;
    a.id = ++next_id;
    assigned.push_back(std::move(a));
  }

  if (!existing) {
    Edge e;
    e.parent = parent;
    e.child = child;
    e.is_static = false;
    e.samples = std::move(assigned);
    edges_[child] = std::move(e);
    children_[parent].push_back(child);
  } else {
    auto& cur = it->second.samples;
    cur.insert(cur.end(), assigned.begin(), assigned.end());
    std::sort(cur.begin(), cur.end(),
              [](const Sample& a, const Sample& b) {
                return a.stamp_us < b.stamp_us;
              });
  }
}

std::vector<std::string> FrameTree::findPathLocked(
    const std::string& from, const std::string& to) const {
  if (edges_.find(from) == edges_.end() &&
      children_.find(from) == children_.end())
    throw TfError(ErrorCode::kFrameNotFound, "unknown frame '" + from + "'");
  if (edges_.find(to) == edges_.end() && children_.find(to) == children_.end())
    throw TfError(ErrorCode::kFrameNotFound, "unknown frame '" + to + "'");

  // Undirected BFS across the forest.
  std::unordered_map<std::string, std::string> came;
  std::queue<std::string> q;
  q.push(from);
  came[from] = "";
  while (!q.empty()) {
    std::string cur = q.front();
    q.pop();
    if (cur == to) break;
    auto neigh = [&](const std::string& n) {
      if (came.emplace(n, cur).second) q.push(n);
    };
    auto pe = edges_.find(cur);
    if (pe != edges_.end()) neigh(pe->second.parent);
    auto ce = children_.find(cur);
    if (ce != children_.end())
      for (const auto& c : ce->second) neigh(c);
  }
  if (came.find(to) == came.end())
    throw TfError(ErrorCode::kDisconnected,
                   "frames '" + from + "' and '" + to +
                       "' are not connected in the tree");

  std::vector<std::string> rev;
  for (std::string c = to; !c.empty(); c = came[c]) rev.push_back(c);
  std::reverse(rev.begin(), rev.end());
  return rev;
}

Transform FrameTree::evalEdgeForTimeLocked(const Edge& e, int64_t t_us,
                                           UsedSample* used) const {
  const auto& s = e.samples;
  if (s.empty())
    throw TfError(ErrorCode::kNoSamples,
                   "dynamic edge '" + e.parent + "'->'" + e.child +
                       "' has no samples yet");

  UsedSample u;
  u.frame = e.child;
  u.is_static = false;
  u.queried_us = t_us;

  // Exact match / before-first / after-last.
  if (t_us < s.front().stamp_us)
    throw TfError(ErrorCode::kOutOfRange,
                   "time " + std::to_string(t_us) +
                       "us precedes first sample " +
                       std::to_string(s.front().stamp_us) + "us on edge '" +
                       e.parent + "'->'" + e.child + "'; extrapolation forbidden");
  if (t_us > s.back().stamp_us)
    throw TfError(ErrorCode::kOutOfRange,
                   "time " + std::to_string(t_us) +
                       "us is after last sample " +
                       std::to_string(s.back().stamp_us) + "us on edge '" +
                       e.parent + "'->'" + e.child + "'; extrapolation forbidden");

  // First sample at or after t.
  auto it = std::lower_bound(
      s.begin(), s.end(), t_us,
      [](const Sample& sm, int64_t v) { return sm.stamp_us < v; });

  u.queried_us = t_us;
  if (it->stamp_us == t_us) {
    u.interpolated = false;
    u.sample_a = u.sample_b = it->id;
    u.stamp_a_us = u.stamp_b_us = it->stamp_us;
    u.alpha = 0.0;
    *used = u;
    return it->xform;
  }
  // Strictly between it-1 and it (both exist given the window checks).
  const Sample& a = *(it - 1);
  const Sample& b = *it;
  const double span =
      static_cast<double>(b.stamp_us - a.stamp_us);
  const double alpha = span > 0.0
                           ? static_cast<double>(t_us - a.stamp_us) / span
                           : 0.0;
  u.interpolated = true;
  u.sample_a = a.id;
  u.sample_b = b.id;
  u.stamp_a_us = a.stamp_us;
  u.stamp_b_us = b.stamp_us;
  u.alpha = alpha;
  *used = u;
  return interpolate(a.xform, b.xform, alpha);
}

Transform FrameTree::evalEdgeLatestLocked(const Edge& e,
                                          UsedSample* used) const {
  if (e.samples.empty())
    throw TfError(ErrorCode::kNoSamples,
                   "dynamic edge '" + e.parent + "'->'" + e.child +
                       "' has no samples yet");
  const Sample& s = e.samples.back();
  UsedSample u;
  u.frame = e.child;
  u.is_static = false;
  u.interpolated = false;
  u.sample_a = u.sample_b = s.id;
  u.stamp_a_us = u.stamp_b_us = s.stamp_us;
  u.queried_us = s.stamp_us;
  *used = u;
  return s.xform;
}

QueryResult FrameTree::queryLocked(const std::string& from,
                                   const std::string& to, int64_t t_us,
                                   bool latest) const {
  std::vector<std::string> path = findPathLocked(from, to);

  QueryResult res;
  res.from_frame = from;
  res.to_frame = to;
  res.latest = latest;
  res.path = path;
  res.xform = Transform{};  // identity
  bool have_bounds = false;

  // Accumulate T_path[i+1]_path[i], i.e. transform mapping coordinates from
  // the `from` frame into the `to` frame.
  for (size_t k = 0; k + 1 < path.size(); ++k) {
    const std::string& a = path[k];
    const std::string& b = path[k + 1];
    Transform local;
    UsedSample u;
    auto pe = edges_.find(b);
    if (pe != edges_.end() && pe->second.parent == a) {
      // Edge runs a->b: use as stored (T_b_a).
      const Edge& e = pe->second;
      if (e.is_static) {
        local = e.static_xform;
        u = {};
        u.frame = b;
        u.is_static = true;
        u.queried_us = t_us;
      } else {
        local = latest ? evalEdgeLatestLocked(e, &u)
                       : evalEdgeForTimeLocked(e, t_us, &u);
      }
    } else {
      // Edge runs b->a (walk upward): use its inverse (T_a_b).
      auto pe2 = edges_.find(a);
      const Edge& e = pe2->second;
      if (e.is_static) {
        local = inverse(e.static_xform);
        u = {};
        u.frame = a;
        u.is_static = true;
        u.queried_us = t_us;
      } else {
        local = latest ? inverse(evalEdgeLatestLocked(e, &u))
                       : inverse(evalEdgeForTimeLocked(e, t_us, &u));
      }
    }
    res.trace.push_back(u);
    res.xform = compose(local, res.xform);

    if (!u.is_static) {
      if (!have_bounds) {
        res.min_sample_time_us = u.stamp_a_us;
        res.max_sample_time_us = u.stamp_b_us;
        have_bounds = true;
      } else {
        res.min_sample_time_us =
            std::min(res.min_sample_time_us, u.stamp_a_us);
        res.max_sample_time_us =
            std::max(res.max_sample_time_us, u.stamp_b_us);
      }
    }
  }

  if (latest) {
    // time error: spread of the actual sample times consumed across the path
    int64_t mn = 0, mx = 0;
    bool any = false;
    for (const auto& u : res.trace) {
      if (u.is_static) continue;
      if (!any) {
        mn = mx = u.stamp_a_us;
        any = true;
      } else {
        mn = std::min(mn, u.stamp_a_us);
        mx = std::max(mx, u.stamp_b_us);
      }
    }
    if (any) res.time_error_us = mx - mn;
    (void)have_bounds;
  }
  // Timed queries are exact with respect to the requested time: interpolation
  // evaluates AT t, so the residual time error is 0 by construction.
  return res;
}

QueryResult FrameTree::query(const std::string& from, const std::string& to,
                             int64_t t_us) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return queryLocked(from, to, t_us, false);
}

QueryResult FrameTree::queryLatest(const std::string& from,
                                   const std::string& to) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return queryLocked(from, to, 0, true);
}

std::vector<std::string> FrameTree::frames() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::vector<std::string> out;
  for (const auto& kv : children_) out.push_back(kv.first);
  std::sort(out.begin(), out.end());
  return out;
}

std::vector<DumpNode> FrameTree::dump() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::vector<DumpNode> out;
  for (const auto& kv : children_) {
    DumpNode d;
    d.frame = kv.first;
    auto it = edges_.find(kv.first);
    if (it != edges_.end()) {
      d.parent = it->second.parent;
      d.is_static = it->second.is_static;
      if (!it->second.is_static && !it->second.samples.empty()) {
        d.sample_count = it->second.samples.size();
        d.first_stamp_us = it->second.samples.front().stamp_us;
        d.last_stamp_us = it->second.samples.back().stamp_us;
      }
    }
    out.push_back(std::move(d));
  }
  std::sort(out.begin(), out.end(),
            [](const DumpNode& a, const DumpNode& b) {
              return a.frame < b.frame;
            });
  return out;
}

}  // namespace tf
