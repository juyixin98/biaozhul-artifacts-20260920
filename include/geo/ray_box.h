// 射线与轴对齐包围盒（AABB）求交。
//
// 射线：P(t) = origin + t * dir, t >= 0。
// dir 内部归一化为单位向量，因此返回的 t_enter / t_exit 均为沿射线的
// 欧氏距离（单位与坐标相同）；调用方传入的 dir 无需预先归一化。
//
// 采用 slab（平行平面区间）算法，逐轴求参数区间 [t_lo_axis, t_hi_axis]，
// 三轴区间相交得 [t_enter, t_exit]。dir 分量为 0 时不做除法，单独处理
// （原点不在该轴 slab 内则必然不相交），从根本上避免 0/0 产生 NaN。
//
// 边界约定：盒为闭区间 [mn, mx]。与盒面/盒棱/盒角精确相切视为命中。
// 浮点比较使用尺度无关容差 eps（默认 1e-12，按 |原点| 与 |命中点| 放大）。
#pragma once

#include "geo/types.h"

namespace geo {

struct Ray {
    Vec3 origin;
    Vec3 dir;       // 单位方向
    Vec3 invDir;    // 0 分量处取 0（不使用倒数），仅供 BVH 加速遍历参考
};

// 由起点与原始方向构造射线；方向为零向量时返回 false（输入层应先行拒绝）。
bool makeRay(const Vec3& origin, const Vec3& dirRaw, Ray& rayOut);

struct RayHit {
    bool   hit = false;
    bool   inside = false;   // 起点位于盒内（含边界容差带）
    double tEnter = 0.0;     // 入射参数（欧氏距离）；起点在盒内时为 0
    double tExit = 0.0;      // 出射参数
};

// 单射线-单盒求交。eps 为相对容差（见 README“精度与容差”）。
RayHit intersectRayAABB(const Ray& ray, const AABB& box, double eps = 1e-12);

// 判断点是否位于闭盒内（含尺度相关容差带）。
bool pointInBox(const Vec3& p, const AABB& box, double eps = 1e-12);

}  // namespace geo
