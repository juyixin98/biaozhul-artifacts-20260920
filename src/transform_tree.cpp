// transform_tree.cpp
#include "transform_tree.hpp"

#include <algorithm>
#include <cmath>

namespace tftree {

namespace {

constexpr int kBadRequest = 400;    // 输入格式/数值错误
constexpr int kConflict = 409;      // 环 / 多父冲突
constexpr int kNotFound = 422;      // 查询路径缺边（语义上不可满足）

}  // namespace

void TransformTree::addStaticEdge(const std::string& parent,
                                  const std::string& child,
                                  const Eigen::Vector3d& p,
                                  Eigen::Quaterniond q) {
    if (parent == child)
        throw Error("self_loop", "frame cannot be its own parent: " + child,
                    kConflict);
    try {
        q = tfmath::validatedQuaternion(q);
    } catch (const std::invalid_argument& e) {
        throw Error("invalid_quaternion", e.what(), kBadRequest);
    }

    std::lock_guard<std::mutex> lk(mu_);
    ensureNoMultiParentLocked(child, parent);
    // 仅当这是一条*新增*的边时才需要环检测；child 已挂在 parent 下时
    // （静态覆盖/动态追加样本）现存边不可能因本次写入成环。
    if (edges_.find(child) == edges_.end())
        ensureNoCycleLocked(parent, child);

    Edge& e = edges_[child];
    e.parent = parent;
    e.is_static = true;
    tfmath::Sample s;
    s.t = 0.0;
    s.p = p;
    s.q = q;
    e.static_sample = s;
    // 静态边与该 child 的动态样本互斥：静态边永远只有一个值。
    e.samples.clear();
}

void TransformTree::addDynamicSample(const std::string& parent,
                                     const std::string& child,
                                     const tfmath::Sample& sampleIn) {
    if (parent == child)
        throw Error("self_loop", "frame cannot be its own parent: " + child,
                    kConflict);
    tfmath::Sample sample = sampleIn;
    try {
        sample.q = tfmath::validatedQuaternion(sample.q);
    } catch (const std::invalid_argument& e) {
        throw Error("invalid_quaternion", e.what(), kBadRequest);
    }
    if (!std::isfinite(sample.t) || !std::isfinite(sample.p.x()) ||
        !std::isfinite(sample.p.y()) || !std::isfinite(sample.p.z()))
        throw Error("invalid_number", "sample contains NaN/Inf", kBadRequest);

    std::lock_guard<std::mutex> lk(mu_);
    ensureNoMultiParentLocked(child, parent);
    if (edges_.find(child) == edges_.end())
        ensureNoCycleLocked(parent, child);

    Edge& e = edges_[child];
    e.parent = parent;
    if (e.is_static && e.static_sample) {
        // child 已有静态边：静态/动态分离管理，不允许混用。
        throw Error("static_dynamic_conflict",
                    "frame '" + child + "' already has a static edge from '" +
                        parent + "'",
                    kConflict);
    }
    e.is_static = false;
    e.static_sample.reset();

    // 有序插入；同时间戳覆盖（异步重发/乱序到达安全）。
    auto it = std::lower_bound(
        e.samples.begin(), e.samples.end(), sample.t,
        [](const tfmath::Sample& s, double t) { return s.t < t; });
    if (it != e.samples.end() && it->t == sample.t)
        *it = sample;
    else
        e.samples.insert(it, sample);
}

bool TransformTree::hasFrame(const std::string& f) const {
    std::lock_guard<std::mutex> lk(mu_);
    return edges_.count(f) > 0 ||
           std::any_of(edges_.begin(), edges_.end(),
                       [&](const auto& kv) { return kv.second.parent == f; });
}

void TransformTree::ensureNoMultiParentLocked(const std::string& child,
                                              const std::string& parent) const {
    auto it = edges_.find(child);
    if (it != edges_.end() && it->second.parent != parent) {
        throw Error("multiple_parent_conflict",
                    "frame '" + child + "' already has parent '" +
                        it->second.parent + "', cannot reparent to '" +
                        parent + "'",
                    kConflict);
    }
}

void TransformTree::ensureNoCycleLocked(const std::string& parent,
                                        const std::string& child) const {
    // 新增 parent->child 会成环，当且仅当 child 已经是 parent 的祖先
    // （沿 parent 向上能走到 child）。
    std::string cur = parent;
    std::set<std::string> seen;
    while (true) {
        if (cur == child)
            throw Error("cycle_detected",
                        "adding edge " + parent + "->" + child +
                            " would create a cycle",
                        kConflict);
        if (!seen.insert(cur).second)
            throw Error("cycle_detected", "existing cycle near frame " + cur,
                        kConflict);
        auto it = edges_.find(cur);
        if (it == edges_.end()) return;  // 到达根，无环
        cur = it->second.parent;
    }
}

std::vector<TransformTree::PathStep>
TransformTree::buildPathLocked(const std::string& source,
                               const std::string& target) const {
    // 树结构下用最近公共祖先 (LCA) 拼路径。
    auto ancestors = [&](std::string f) {
        std::vector<std::string> chain;
        std::set<std::string> seen;
        while (true) {
            chain.push_back(f);
            if (!seen.insert(f).second)
                throw Error("cycle_detected",
                            "cycle in tree at frame '" + f + "'", 409);
            auto it = edges_.find(f);
            if (it == edges_.end()) break;  // f 是根
            f = it->second.parent;
        }
        return chain;
    };

    std::vector<std::string> a = ancestors(source);
    std::vector<std::string> b = ancestors(target);

    // 找公共祖先：a/b 均从自身向根排列。
    std::set<std::string> inA(a.begin(), a.end());
    std::string lca;
    for (const auto& f : b)
        if (inA.count(f)) { lca = f; break; }
    if (lca.empty())
        throw Error("disconnected_tree",
                    "frames '" + source + "' and '" + target +
                        "' belong to different trees",
                    kNotFound);

    std::vector<PathStep> steps;
    // source 向上走到 lca：每一步沿 child->parent，查询时取逆。
    for (std::size_t i = 0; i + 1 < a.size() && a[i] != lca; ++i) {
        // a[i] 是 child，a[i+1] 是 parent；边存储为 child=a[i]
        steps.push_back({a[i + 1], a[i], /*inverted=*/true});
        if (a[i + 1] == lca) break;
    }
    // lca 向下走到 target：b = [target, ..., lca, ...root]，反向遍历。
    std::size_t lcaIdxB = 0;
    while (lcaIdxB < b.size() && b[lcaIdxB] != lca) ++lcaIdxB;
    for (std::size_t i = lcaIdxB; i > 0; --i) {
        // 边 child=b[i-1], parent=b[i]，沿 parent->child 正向使用。
        steps.push_back({b[i], b[i - 1], /*inverted=*/false});
    }
    return steps;
}

TransformTree::EdgeEval
TransformTree::evalEdgeLocked(const Edge& e, const std::string& parent,
                              const std::string& child, bool inverted,
                              double time, double boundary_tol) const {
    EdgeEval out;
    out.info.parent = parent;
    out.info.child = child;
    out.info.inverted = inverted;
    out.info.is_static = e.is_static;
    out.info.requested_time = time;
    out.info.mode = "exact";

    auto emit = [&](const tfmath::Sample& s) {
        out.sample = inverted ? tfmath::invertSample(s) : s;
    };

    if (e.is_static) {
        if (!e.static_sample)
            throw Error("internal_error", "static edge without sample", 500);
        out.info.mode = "static";
        emit(*e.static_sample);
        return out;
    }

    if (e.samples.empty())
        throw Error("edge_without_data",
                    "dynamic edge " + parent + "->" + child + " has no samples",
                    kNotFound);

    const auto& sv = e.samples;
    auto it = std::lower_bound(
        sv.begin(), sv.end(), time,
        [](const tfmath::Sample& s, double t) { return s.t < t; });

    if (it != sv.end() && it->t == time) {
        out.info.sample_times = {time};
        out.info.time_error = 0.0;
        emit(*it);
        return out;
    }

    if (it == sv.begin()) {
        // 请求时间早于最早样本。
        double gap = sv.front().t - time;
        if (boundary_tol > 0.0 && gap <= boundary_tol) {
            out.info.mode = "clamped_earliest";
            out.info.sample_times = {sv.front().t};
            out.info.time_error = gap;
            emit(sv.front());
            return out;
        }
        throw Error("extrapolation_forbidden",
                    "time " + std::to_string(time) +
                        " is before earliest sample " +
                        std::to_string(sv.front().t) + " on edge " + parent +
                        "->" + child + "; extrapolation is not allowed",
                    kNotFound);
    }

    if (it == sv.end()) {
        double gap = time - sv.back().t;
        if (boundary_tol > 0.0 && gap <= boundary_tol) {
            out.info.mode = "clamped_latest";
            out.info.sample_times = {sv.back().t};
            out.info.time_error = gap;
            emit(sv.back());
            return out;
        }
        throw Error("extrapolation_forbidden",
                    "time " + std::to_string(time) +
                        " is after latest sample " +
                        std::to_string(sv.back().t) + " on edge " + parent +
                        "->" + child + "; extrapolation is not allowed",
                    kNotFound);
    }

    // 区间内线性/球面插值。
    const tfmath::Sample& hi = *it;
    const tfmath::Sample& lo = *(it - 1);
    tfmath::Sample interp = tfmath::interpolate(lo, hi, time);
    out.info.mode = "interpolated";
    out.info.sample_times = {lo.t, hi.t};
    // 时间误差 = 到最近使用样本的距离（整条链再取各边最大值）。
    out.info.time_error = std::min(time - lo.t, hi.t - time);
    emit(interp);
    return out;
}

QueryResult TransformTree::query(const std::string& source,
                                 const std::string& target, double time,
                                 double boundary_tol) const {
    if (source.empty() || target.empty())
        throw Error("invalid_frame", "frame name must not be empty",
                    kBadRequest);

    std::lock_guard<std::mutex> lk(mu_);
    QueryResult result;
    result.time = time;

    if (source == target) {
        result.translation = Eigen::Vector3d::Zero();
        result.rotation = Eigen::Quaterniond::Identity();
        result.matrix.setIdentity();
        return result;
    }

    auto frameExists = [&](const std::string& f) {
        if (edges_.count(f)) return true;
        return std::any_of(edges_.begin(), edges_.end(),
                           [&](const auto& kv) { return kv.second.parent == f; });
    };
    if (!frameExists(source))
        throw Error("unknown_frame", "unknown frame: " + source, kNotFound);
    if (!frameExists(target))
        throw Error("unknown_frame", "unknown frame: " + target, kNotFound);

    std::vector<PathStep> path = buildPathLocked(source, target);

    Eigen::Isometry3d total = Eigen::Isometry3d::Identity();
    for (const PathStep& step : path) {
        const Edge& e = edges_.at(step.child);
        if (e.parent != step.parent)
            throw Error("internal_error", "path/edge mismatch", 500);
        EdgeEval ev = evalEdgeLocked(e, step.parent, step.child,
                                     step.inverted, time, boundary_tol);
        total = total * tfmath::toIsometry(ev.sample);
        result.max_time_error =
            std::max(result.max_time_error, ev.info.time_error);
        result.edges.push_back(std::move(ev.info));
    }

    result.translation = total.translation();
    result.rotation = Eigen::Quaterniond(total.linear());
    result.rotation.normalize();
    result.matrix = total.matrix();
    return result;
}

std::vector<TransformTree::FrameInfo> TransformTree::listFrames() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::set<std::string> all;
    for (const auto& [child, e] : edges_) {
        all.insert(child);
        all.insert(e.parent);
    }
    std::vector<FrameInfo> out;
    for (const std::string& f : all) {
        FrameInfo fi;
        fi.name = f;
        auto it = edges_.find(f);
        if (it == edges_.end()) {
            fi.parent = "";  // 根
        } else {
            fi.parent = it->second.parent;
            fi.is_static = it->second.is_static;
            fi.sample_count = it->second.samples.size();
            if (!fi.is_static && !it->second.samples.empty()) {
                fi.t_min = it->second.samples.front().t;
                fi.t_max = it->second.samples.back().t;
            }
        }
        out.push_back(std::move(fi));
    }
    std::sort(out.begin(), out.end(),
              [](const FrameInfo& a, const FrameInfo& b) {
                  return a.name < b.name;
              });
    return out;
}

}  // namespace tftree
