// 基于轴对齐包围盒的 BVH（层次包围体）。
//
// 构建：最长轴 + 中位数划分（nth_element），叶子最多 LEAF_SIZE 个盒。
// 遍历：显式栈、无递归深度风险；节点盒同样走 slab 测试（零分量单独处理），
//       带与逐盒检测一致的容差，保证不会因舍入漏掉本应命中的叶子。
//
// 提供三种结果口径：
//   nearest  —— 最近命中（tEnter 最小，并列取较小 id）
//   allHits  —— 全部命中（按 tEnter 升序、id 升序）
//   brute*   —— 逐盒检测参照实现，供测试与一致性核对
#pragma once

#include <cstdint>
#include <vector>

#include "geo/ray_box.h"
#include "geo/types.h"

namespace geo {

struct BoxInput {
    int64_t id;
    AABB box;
};

struct Hit {
    int64_t id = 0;
    double tEnter = 0.0;
    double tExit = 0.0;
    bool   inside = false;
    Vec3   pointEnter;
    Vec3   pointExit;
};

struct TraversalStats {
    int64_t nodesVisited = 0;  // 内部节点（含根）做 slab 测试的次数
    int64_t boxesTested = 0;   // 叶子内逐盒求交次数
};

class BVH {
public:
    static constexpr int LEAF_SIZE = 4;

    void build(const std::vector<BoxInput>& boxes, double eps = 1e-12);

    // 最近命中；未命中时返回值 hit=false（Hit 中用 id == INT64_MIN 标记，
    // 另通过返回值/命中指针给出）。stats 可为 nullptr。
    bool nearest(const Ray& ray, Hit& outHit,
                 TraversalStats* stats = nullptr) const;

    // 全部命中，按 (tEnter, id) 升序。
    std::vector<Hit> allHits(const Ray& ray,
                             TraversalStats* stats = nullptr) const;

    // —— 逐盒检测参照实现 ——
    bool bruteNearest(const Ray& ray, Hit& outHit) const;
    std::vector<Hit> bruteAll(const Ray& ray) const;

    int64_t nodeCount() const { return static_cast<int64_t>(nodes_.size()); }
    int64_t boxCount() const { return static_cast<int64_t>(items_.size()); }

private:
    struct Node {
        AABB bounds;
        int32_t left = -1;    // >=0：内部节点子节点索引
        int32_t right = -1;
        int32_t first = 0;    // 叶子：items_ 起始下标
        int32_t count = 0;    // 叶子：盒数；0 表示内部节点
    };

    struct Item {
        int64_t id;
        AABB box;
        Vec3 center;
    };

    int32_t buildRecursive(std::vector<int32_t>& indices, int begin, int end);
    void makeHit(const Ray& ray, const Item& item, const RayHit& rh,
                 Hit& out) const;
    bool collectBrute(const Ray& ray, Hit& nearestOut,
                      std::vector<Hit>* allOut) const;

    double eps_ = 1e-12;
    std::vector<Node> nodes_;
    std::vector<Item> items_;
};

}  // namespace geo
