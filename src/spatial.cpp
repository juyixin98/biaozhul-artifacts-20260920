// spatial.cpp — 静态二维 KD 树实现
//
// 构建：递归按深度交替选择分割轴（depth 偶数取 x，奇数取 y），
//       用 nth_element 取中位数，保证树高 O(log N)。所有坐标相同等退化情形
//       仍按 id 次序完成平衡划分，树不会退化成链。
//
// KNN 剪枝：维护大小为 k 的堆，堆顶为当前“最差”（距离/id 最大）候选。
//   访问分割点另一侧子树的条件是 分割平面距离² <= 当前最差距离² ——
//   取等号时也必须进入，因为平面另一侧可能存在距离相同、但 id 更小的点。
//
// 半径查询：远侧子树仅在 平面距离² <= r² 时进入（边界包含）。
#include "spatial.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace spatial {

long double distSq(long double ax, long double ay, long double bx, long double by) {
    long double dx = ax - bx;
    long double dy = ay - by;
    // 坐标在入口处已保证有限；即使差值巨大，乘法至多得到 +inf（良定义），
    // 不会产生 NaN/未定义行为。
    long double sx = dx * dx;
    long double sy = dy * dy;
    if (!std::isfinite(sx)) return std::numeric_limits<long double>::infinity();
    if (!std::isfinite(sy)) return std::numeric_limits<long double>::infinity();
    long double s = sx + sy;
    if (!std::isfinite(s)) return std::numeric_limits<long double>::infinity();
    return s;
}

bool better(const Candidate& a, const Candidate& b) {
    if (a.d2 != b.d2) return a.d2 < b.d2;
    return a.id < b.id;
}

namespace {
// 堆比较器：堆顶是“最差”候选（距离最大；距离相同则 id 最大）。
// better(a,b) 为真表示 a 更优、优先级更低，因此堆顶自然是最差者。
struct HeapCmp {
    bool operator()(const Candidate& a, const Candidate& b) const {
        return better(a, b);
    }
};

bool coordLess(int axis, const Point& a, const Point& b) {
    long double va = (axis == 0 ? a.x : a.y);
    long double vb = (axis == 0 ? b.x : b.y);
    if (va != vb) return va < vb;
    return a.id < b.id;  // 坐标相等时以 id 保证严格弱序与划分确定性
}
}  // namespace

KdTree::KdTree(std::vector<Point> points) {
    build(std::move(points));
}

void KdTree::build(std::vector<Point> points) {
    points_ = std::move(points);
    nodes_.clear();
    nodes_.reserve(points_.size());
    root_ = points_.empty() ? -1 : buildRec(0, points_.size(), 0);
}

int KdTree::buildRec(std::size_t begin, std::size_t end, int depth) {
    if (begin >= end) return -1;
    int axis = depth % 2;
    std::size_t mid = begin + (end - begin) / 2;

    std::nth_element(points_.begin() + static_cast<std::ptrdiff_t>(begin),
                     points_.begin() + static_cast<std::ptrdiff_t>(mid),
                     points_.begin() + static_cast<std::ptrdiff_t>(end),
                     [axis](const Point& a, const Point& b) { return coordLess(axis, a, b); });

    Node node;
    node.begin = begin;
    node.end = end;
    node.split = mid;
    node.axis = axis;
    int self = static_cast<int>(nodes_.size());
    nodes_.push_back(node);  // 先占位，递归中 nodes_ 重排不影响下标
    // 点正好位于分割平面上时（delta==0），统一把左侧当作近侧，保证遍历顺序确定。
    nodes_[self].left = buildRec(begin, mid, depth + 1);
    nodes_[self].right = buildRec(mid + 1, end, depth + 1);
    return self;
}

std::vector<Candidate> KdTree::kNearest(const XY& q, std::size_t k, bool* truncated) const {
    if (truncated) *truncated = (k > points_.size());
    if (k == 0 || points_.empty()) return {};
    std::vector<Candidate> heap;
    heap.reserve(std::min(k, points_.size()));
    knnRec(root_, q, k, heap, truncated);
    std::sort(heap.begin(), heap.end(),
              [](const Candidate& a, const Candidate& b) { return better(a, b); });
    return heap;
}

void KdTree::knnRec(int nodeIdx, const XY& q, std::size_t k,
                   std::vector<Candidate>& heap, bool* /*truncated*/) const {
    if (nodeIdx < 0) return;
    const Node& node = nodes_[static_cast<std::size_t>(nodeIdx)];
    const Point& p = points_[node.split];

    Candidate c;
    c.id = p.id;
    c.x = p.x;
    c.y = p.y;
    c.d2 = distSq(p.x, p.y, q.x, q.y);

    if (heap.size() < k) {
        heap.push_back(c);
        std::push_heap(heap.begin(), heap.end(), HeapCmp{});
    } else if (better(c, heap.front())) {
        // 严格更优才替换；距离相同且 id 更大者不替换当前候选
        std::pop_heap(heap.begin(), heap.end(), HeapCmp{});
        heap.pop_back();
        heap.push_back(c);
        std::push_heap(heap.begin(), heap.end(), HeapCmp{});
    }

    long double splitCoord = (node.axis == 0 ? p.x : p.y);
    long double qCoord = (node.axis == 0 ? q.x : q.y);
    long double delta = qCoord - splitCoord;
    int nearChild = (delta <= 0.0L ? node.left : node.right);
    int farChild = (delta <= 0.0L ? node.right : node.left);

    knnRec(nearChild, q, k, heap, nullptr);

    // 近侧遍历后堆中最差距离可能已缩小，此时再用最新最差距离判定远侧剪枝。
    // 取等号也必须进入远侧：分割平面另一侧可能存在距离相同、但 id 更小的点。
    bool mustVisitFar = (heap.size() < k);
    if (!mustVisitFar) {
        long double planeD2 = delta * delta;
        if (!std::isfinite(planeD2)) planeD2 = std::numeric_limits<long double>::infinity();
        mustVisitFar = (planeD2 <= heap.front().d2);
    }
    if (mustVisitFar) knnRec(farChild, q, k, heap, nullptr);
}

bool KdTree::radiusQuery(const XY& q, long double radius, std::vector<Candidate>& out) const {
    if (!std::isfinite(radius) || radius < 0.0L) return false;  // NaN 或负数非法
    long double r2 = radius * radius;
    if (!std::isfinite(r2)) r2 = std::numeric_limits<long double>::infinity();
    if (points_.empty()) return true;
    radiusRec(root_, q, r2, out);
    std::sort(out.begin(), out.end(),
              [](const Candidate& a, const Candidate& b) { return better(a, b); });
    return true;
}

void KdTree::radiusRec(int nodeIdx, const XY& q, long double r2,
                       std::vector<Candidate>& out) const {
    if (nodeIdx < 0) return;
    const Node& node = nodes_[static_cast<std::size_t>(nodeIdx)];
    const Point& p = points_[node.split];

    long double d2 = distSq(p.x, p.y, q.x, q.y);
    if (d2 <= r2) {  // 边界包含：距离恰好等于半径也返回
        Candidate c{p.id, p.x, p.y, d2};
        out.push_back(c);
    }

    long double splitCoord = (node.axis == 0 ? p.x : p.y);
    long double qCoord = (node.axis == 0 ? q.x : q.y);
    long double delta = qCoord - splitCoord;
    int nearChild = (delta <= 0.0L ? node.left : node.right);
    int farChild = (delta <= 0.0L ? node.right : node.left);

    // 查询点所在的半空间，最小距离为 0，必须递归。
    radiusRec(nearChild, q, r2, out);

    long double planeD2 = delta * delta;
    if (!std::isfinite(planeD2)) planeD2 = std::numeric_limits<long double>::infinity();
    // 远侧半空间中任意点到查询点的距离至少为 |delta|；等于时不剪枝。
    if (planeD2 <= r2) radiusRec(farChild, q, r2, out);
}

// ---------------- 朴素参照实现（全扫描） ----------------

std::vector<Candidate> bruteKNN(const std::vector<Point>& points, const XY& q, std::size_t k) {
    std::vector<Candidate> all;
    all.reserve(points.size());
    for (const auto& p : points) {
        all.push_back(Candidate{p.id, p.x, p.y, distSq(p.x, p.y, q.x, q.y)});
    }
    std::sort(all.begin(), all.end(),
              [](const Candidate& a, const Candidate& b) { return better(a, b); });
    if (all.size() > k) all.resize(k);
    return all;
}

std::vector<Candidate> bruteRadius(const std::vector<Point>& points, const XY& q,
                                   long double radius) {
    std::vector<Candidate> out;
    if (!std::isfinite(radius) || radius < 0.0L) return out;
    long double r2 = radius * radius;
    if (!std::isfinite(r2)) r2 = std::numeric_limits<long double>::infinity();
    for (const auto& p : points) {
        long double d2 = distSq(p.x, p.y, q.x, q.y);
        if (d2 <= r2) out.push_back(Candidate{p.id, p.x, p.y, d2});
    }
    std::sort(out.begin(), out.end(),
              [](const Candidate& a, const Candidate& b) { return better(a, b); });
    return out;
}

}  // namespace spatial
