#include "geo/bvh.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace geo {

namespace {

Vec3 pointAt(const Ray& ray, double t) {
    return ray.origin + ray.dir * t;
}

}  // namespace

void BVH::build(const std::vector<BoxInput>& boxes, double eps) {
    eps_ = eps;
    nodes_.clear();
    items_.clear();

    items_.reserve(boxes.size());
    for (const auto& bi : boxes) {
        Item it;
        it.id = bi.id;
        it.box = bi.box;
        for (int a = 0; a < 3; ++a) {
            it.center[a] = bi.box.mn[a] + 0.5 * (bi.box.mx[a] - bi.box.mn[a]);
        }
        items_.push_back(it);
    }

    if (items_.empty()) return;

    std::vector<int32_t> indices(items_.size());
    for (int32_t i = 0; i < static_cast<int32_t>(indices.size()); ++i) {
        indices[i] = i;
    }
    buildRecursive(indices, 0, static_cast<int>(indices.size()));

    // 节点叶子以位置区间 [first, first+count) 索引，故建树后必须按
    // 划分排列重排 items_（划分只动了 indices）。
    std::vector<Item> ordered;
    ordered.resize(items_.size());
    for (size_t i = 0; i < indices.size(); ++i) {
        ordered[i] = std::move(items_[indices[i]]);
    }
    items_.swap(ordered);
}

int32_t BVH::buildRecursive(std::vector<int32_t>& indices, int begin, int end) {
    AABB bounds;
    bounds.mn = Vec3(std::numeric_limits<double>::infinity(),
                     std::numeric_limits<double>::infinity(),
                     std::numeric_limits<double>::infinity());
    bounds.mx = Vec3(-std::numeric_limits<double>::infinity(),
                     -std::numeric_limits<double>::infinity(),
                     -std::numeric_limits<double>::infinity());
    Vec3 cLo(items_[indices[begin]].center);
    Vec3 cHi(items_[indices[begin]].center);
    for (int i = begin; i < end; ++i) {
        const AABB& b = items_[indices[i]].box;
        for (int a = 0; a < 3; ++a) {
            bounds.mn[a] = std::min(bounds.mn[a], b.mn[a]);
            bounds.mx[a] = std::max(bounds.mx[a], b.mx[a]);
            cLo[a] = std::min(cLo[a], items_[indices[i]].center[a]);
            cHi[a] = std::max(cHi[a], items_[indices[i]].center[a]);
        }
    }

    const int count = end - begin;
    if (count <= LEAF_SIZE) {
        Node leaf;
        leaf.bounds = bounds;
        leaf.first = begin;
        leaf.count = count;
        nodes_.push_back(leaf);
        return static_cast<int32_t>(nodes_.size() - 1);
    }

    // 最长中心跨度轴划分；跨度全为 0（重合盒）时退化为 x 轴，
    // nth_element 仍保证两侧下标范围非空。
    int axis = 0;
    double bestExtent = cHi[0] - cLo[0];
    for (int a = 1; a < 3; ++a) {
        const double ext = cHi[a] - cLo[a];
        if (ext > bestExtent) {
            bestExtent = ext;
            axis = a;
        }
    }
    const int mid = begin + count / 2;
    std::nth_element(indices.begin() + begin, indices.begin() + mid,
                     indices.begin() + end,
                     [&](int32_t i, int32_t j) {
                         return items_[i].center[axis] < items_[j].center[axis];
                     });

    const int32_t left = buildRecursive(indices, begin, mid);
    const int32_t right = buildRecursive(indices, mid, end);

    Node node;
    node.bounds = bounds;
    node.left = left;
    node.right = right;
    node.count = 0;
    nodes_.push_back(node);
    return static_cast<int32_t>(nodes_.size() - 1);
}

void BVH::makeHit(const Ray& ray, const Item& item, const RayHit& rh,
                  Hit& out) const {
    out.id = item.id;
    out.tEnter = rh.tEnter;
    out.tExit = rh.tExit;
    out.inside = rh.inside;
    out.pointEnter = pointAt(ray, rh.tEnter);
    out.pointExit = pointAt(ray, rh.tExit);
}

bool BVH::nearest(const Ray& ray, Hit& outHit, TraversalStats* stats) const {
    if (stats) *stats = TraversalStats();
    if (nodes_.empty()) return false;

    bool found = false;
    double bestT = std::numeric_limits<double>::infinity();
    int64_t bestId = std::numeric_limits<int64_t>::max();

    std::vector<int32_t> stack;
    stack.reserve(nodes_.size() * 2);
    stack.push_back(static_cast<int32_t>(nodes_.size() - 1));

    while (!stack.empty()) {
        const int32_t ni = stack.back();
        stack.pop_back();
        const Node& node = nodes_[ni];
        if (stats) ++stats->nodesVisited;

        const RayHit nh = intersectRayAABB(ray, node.bounds, eps_);
        if (!nh.hit) continue;
        // 严格大于当前最近参数才剪枝；相等时必须进入，并列取较小 id。
        if (nh.tEnter > bestT) continue;

        if (node.count > 0) {
            for (int k = 0; k < node.count; ++k) {
                const Item& item = items_[node.first + k];
                if (stats) ++stats->boxesTested;
                const RayHit lh = intersectRayAABB(ray, item.box, eps_);
                if (!lh.hit) continue;
                // 防御性 NaN 过滤：NaN 参与任何比较都为 false，会污染排序与
                // “最近”判定，这里直接丢弃。
                if (!std::isfinite(lh.tEnter) || !std::isfinite(lh.tExit)) {
                    continue;
                }
                if (lh.tEnter < bestT ||
                    (lh.tEnter == bestT && item.id < bestId)) {
                    bestT = lh.tEnter;
                    bestId = item.id;
                    makeHit(ray, item, lh, outHit);
                    found = true;
                }
            }
        } else {
            // 先压远侧后压近侧，使近侧优先弹出来收紧 bestT。
            const RayHit hl =
                intersectRayAABB(ray, nodes_[node.left].bounds, eps_);
            const RayHit hr =
                intersectRayAABB(ray, nodes_[node.right].bounds, eps_);
            if (hl.hit && hr.hit) {
                if (hl.tEnter <= hr.tEnter) {
                    stack.push_back(node.right);
                    stack.push_back(node.left);
                } else {
                    stack.push_back(node.left);
                    stack.push_back(node.right);
                }
            } else {
                if (hr.hit) stack.push_back(node.right);
                if (hl.hit) stack.push_back(node.left);
            }
        }
    }
    return found;
}

std::vector<Hit> BVH::allHits(const Ray& ray, TraversalStats* stats) const {
    std::vector<Hit> hits;
    if (stats) *stats = TraversalStats();
    if (nodes_.empty()) return hits;

    std::vector<int32_t> stack;
    stack.reserve(nodes_.size() * 2);
    stack.push_back(static_cast<int32_t>(nodes_.size() - 1));

    while (!stack.empty()) {
        const int32_t ni = stack.back();
        stack.pop_back();
        const Node& node = nodes_[ni];
        if (stats) ++stats->nodesVisited;

        const RayHit nh = intersectRayAABB(ray, node.bounds, eps_);
        if (!nh.hit) continue;  // all 模式不能再按最近距离剪枝

        if (node.count > 0) {
            for (int k = 0; k < node.count; ++k) {
                const Item& item = items_[node.first + k];
                if (stats) ++stats->boxesTested;
                const RayHit lh = intersectRayAABB(ray, item.box, eps_);
                if (!lh.hit) continue;
                if (!std::isfinite(lh.tEnter) || !std::isfinite(lh.tExit)) {
                    continue;  // 杜绝 NaN 进入结果列表
                }
                Hit h;
                makeHit(ray, item, lh, h);
                hits.push_back(h);
            }
        } else {
            stack.push_back(node.left);
            stack.push_back(node.right);
        }
    }

    std::sort(hits.begin(), hits.end(), [](const Hit& a, const Hit& b) {
        if (a.tEnter != b.tEnter) return a.tEnter < b.tEnter;
        return a.id < b.id;
    });
    return hits;
}

bool BVH::collectBrute(const Ray& ray, Hit& nearestOut,
                       std::vector<Hit>* allOut) const {
    bool found = false;
    double bestT = std::numeric_limits<double>::infinity();
    int64_t bestId = std::numeric_limits<int64_t>::max();

    for (const Item& item : items_) {
        const RayHit lh = intersectRayAABB(ray, item.box, eps_);
        if (!lh.hit) continue;
        if (!std::isfinite(lh.tEnter) || !std::isfinite(lh.tExit)) continue;
        Hit h;
        makeHit(ray, item, lh, h);
        if (allOut) allOut->push_back(h);
        if (lh.tEnter < bestT || (lh.tEnter == bestT && item.id < bestId)) {
            bestT = lh.tEnter;
            bestId = item.id;
            nearestOut = h;
            found = true;
        }
    }

    if (allOut) {
        std::sort(allOut->begin(), allOut->end(),
                  [](const Hit& a, const Hit& b) {
                      if (a.tEnter != b.tEnter) return a.tEnter < b.tEnter;
                      return a.id < b.id;
                  });
    }
    return found;
}

bool BVH::bruteNearest(const Ray& ray, Hit& outHit) const {
    return collectBrute(ray, outHit, nullptr);
}

std::vector<Hit> BVH::bruteAll(const Ray& ray) const {
    std::vector<Hit> hits;
    Hit unused;
    collectBrute(ray, unused, &hits);
    return hits;
}

}  // namespace geo
