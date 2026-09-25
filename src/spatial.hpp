// spatial.hpp — 静态二维 KD 树：构建、K 近邻查询、半径查询
//
// 坐标系约定（详见 README）：
//   二维笛卡尔平面直角坐标系（x 轴向右、y 轴向上，右手系）。
//   坐标与距离均为无量纲数值；本程序不做任何投影/大地测量换算。
//   距离度量：欧氏距离。内部统一比较“平方距离”以避免开方带来的误差与开销。
//
// 顺序约定（距离并列）：
//   结果按 (平方距离升序, id 升序) 排序，即距离相同时 id 更小者在前。
#pragma once

#include <cstdint>
#include <vector>

namespace spatial {

using Id = std::int64_t;

struct Point {
    Id id = 0;
    long double x = 0.0L;
    long double y = 0.0L;
};

struct XY {
    long double x = 0.0L;
    long double y = 0.0L;
};

// 查询结果项。d2 为平方距离；调用方可开方得到欧氏距离。
struct Candidate {
    Id id = 0;
    long double x = 0.0L;
    long double y = 0.0L;
    long double d2 = 0.0L;  // 平方距离
};

// 平方距离；输入坐标差过大时返回 +inf 而不是产生未定义行为。
long double distSq(long double ax, long double ay, long double bx, long double by);

// 判定 a 是否应排在 b 前：平方距离更小；平方距离相等时 id 更小。
bool better(const Candidate& a, const Candidate& b);

class KdTree {
public:
    KdTree() = default;
    explicit KdTree(std::vector<Point> points);

    // 替换数据并构建索引。重复坐标不会被去重（视为不同点，距离并列按 id 排序）。
    void build(std::vector<Point> points);

    size_t size() const { return points_.size(); }

    // K 近邻。
    // k == 0 返回空；k > 点数 N 时返回全部 N 个点（按距离、id 排序），truncated 置 true。
    std::vector<Candidate> kNearest(const XY& q, std::size_t k, bool* truncated = nullptr) const;

    // 半径查询：返回所有满足 欧氏距离 <= radius 的点（边界包含），按距离、id 排序。
    // radius < 0 视为非法，返回 false；radius 过大导致半径平方溢出时按“覆盖全部”处理。
    bool radiusQuery(const XY& q, long double radius, std::vector<Candidate>& out) const;

private:
    struct Node {
        std::size_t begin = 0;   // 节点对应点索引区间 [begin, end)
        std::size_t end = 0;
        std::size_t split = 0;   // 分割点在 points_ 中的下标
        int axis = 0;            // 0 = 按 x，1 = 按 y
        int left = -1;           // 左子树节点下标
        int right = -1;          // 右子树节点下标
    };

    std::vector<Point> points_;
    std::vector<Node> nodes_;
    int root_ = -1;

    int buildRec(std::size_t begin, std::size_t end, int depth);
    void knnRec(int nodeIdx, const XY& q, std::size_t k,
                std::vector<Candidate>& heap, bool* truncated) const;
    void radiusRec(int nodeIdx, const XY& q, long double r2,
                   std::vector<Candidate>& out) const;
};

// ---- 朴素全扫描参照实现（测试用，逻辑刻意简单直白）----
std::vector<Candidate> bruteKNN(const std::vector<Point>& points, const XY& q, std::size_t k);
std::vector<Candidate> bruteRadius(const std::vector<Point>& points, const XY& q, long double radius);

}  // namespace spatial
