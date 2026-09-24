// Point cloud registration service — kd-tree for 3D nearest-neighbor queries.
// Real implementation: balanced median-split kd-tree, exact nearest neighbour
// (and radius collection) over Euclidean distance.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <numeric>
#include <vector>

#include <Eigen/Dense>

namespace pcrs {

class KdTree {
public:
    explicit KdTree(const std::vector<Eigen::Vector3d>& points)
        : points_(points), nodes_(points.size()) {
        std::vector<int32_t> idx(points.size());
        std::iota(idx.begin(), idx.end(), 0);
        if (!idx.empty()) {
            root_ = build(idx.data(), static_cast<int32_t>(idx.size()), 0);
        }
    }

    // Returns nearest-neighbour index and squared distance. Empty tree -> (-1, inf).
    std::pair<int32_t, double> nearest(const Eigen::Vector3d& q) const {
        if (nodes_.empty()) return {-1, std::numeric_limits<double>::infinity()};
        int32_t best = -1;
        double best_d2 = std::numeric_limits<double>::infinity();
        nn(root_, q, best, best_d2);
        return {best, best_d2};
    }

    // Collect all points with squared distance <= r2.
    std::vector<int32_t> radius(const Eigen::Vector3d& q, double r2) const {
        std::vector<int32_t> hits;
        if (!nodes_.empty()) radius(root_, q, r2, hits);
        return hits;
    }

private:
    struct Node {
        int32_t point = -1;
        int32_t left = -1;
        int32_t right = -1;
        int axis = 0;
    };

    const std::vector<Eigen::Vector3d>& points_;
    std::vector<Node> nodes_;
    int32_t root_ = -1;
    int32_t next_ = 0;

    int32_t build(int32_t* idx, int32_t n, int depth) {
        if (n <= 0) return -1;
        const int axis = depth % 3;
        const int32_t mid = n / 2;
        std::nth_element(idx, idx + mid, idx + n,
                         [&](int32_t a, int32_t b) {
                             return points_[a][axis] < points_[b][axis];
                         });
        const int32_t id = next_++;
        nodes_[id].point = idx[mid];
        nodes_[id].axis = axis;
        nodes_[id].left = build(idx, mid, depth + 1);
        nodes_[id].right = build(idx + mid + 1, n - mid - 1, depth + 1);
        return id;
    }

    void nn(int32_t id, const Eigen::Vector3d& q, int32_t& best,
            double& best_d2) const {
        if (id < 0) return;
        const Node& node = nodes_[id];
        const Eigen::Vector3d& p = points_[node.point];
        const double d2 = (p - q).squaredNorm();
        if (d2 < best_d2) {
            best_d2 = d2;
            best = node.point;
        }
        const double delta = q[node.axis] - p[node.axis];
        const int32_t near_child = delta < 0 ? node.left : node.right;
        const int32_t far_child = delta < 0 ? node.right : node.left;
        nn(near_child, q, best, best_d2);
        if (delta * delta < best_d2) nn(far_child, q, best, best_d2);
    }

    void radius(int32_t id, const Eigen::Vector3d& q, double r2,
                std::vector<int32_t>& hits) const {
        if (id < 0) return;
        const Node& node = nodes_[id];
        const Eigen::Vector3d& p = points_[node.point];
        if ((p - q).squaredNorm() <= r2) hits.push_back(node.point);
        const double delta = q[node.axis] - p[node.axis];
        const int32_t near_child = delta < 0 ? node.left : node.right;
        const int32_t far_child = delta < 0 ? node.right : node.left;
        radius(near_child, q, r2, hits);
        if (delta * delta <= r2) radius(far_child, q, r2, hits);
    }
};

}  // namespace pcrs
