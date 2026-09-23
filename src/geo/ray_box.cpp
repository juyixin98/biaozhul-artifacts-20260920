#include "geo/ray_box.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace geo {

namespace {

// 尺度相关绝对容差：对坐标量级为 s 的比较，允许 eps * max(1, s) 的偏差。
double scaleTol(double eps, double s) {
    return eps * std::max(1.0, std::fabs(s));
}

}  // namespace

bool makeRay(const Vec3& origin, const Vec3& dirRaw, Ray& rayOut) {
    if (!allFinite(origin) || !allFinite(dirRaw)) return false;
    const double len = length(dirRaw);
    if (!(len > 0.0) || !std::isfinite(len)) return false;  // 零向量与非有限值
    rayOut.origin = origin;
    rayOut.dir = dirRaw / len;
    rayOut.invDir = Vec3(0.0, 0.0, 0.0);
    for (int a = 0; a < 3; ++a) {
        if (rayOut.dir[a] != 0.0) rayOut.invDir[a] = 1.0 / rayOut.dir[a];
    }
    return true;
}

bool pointInBox(const Vec3& p, const AABB& box, double eps) {
    for (int a = 0; a < 3; ++a) {
        const double tol = scaleTol(
            eps, std::max(std::fabs(box.mn[a]), std::fabs(box.mx[a])));
        if (p[a] < box.mn[a] - tol || p[a] > box.mx[a] + tol) return false;
    }
    return true;
}

RayHit intersectRayAABB(const Ray& ray, const AABB& box, double eps) {
    RayHit result;  // 默认 hit = false

    double tEnter = -std::numeric_limits<double>::infinity();
    double tExit = std::numeric_limits<double>::infinity();

    for (int a = 0; a < 3; ++a) {
        const double o = ray.origin[a];
        const double d = ray.dir[a];
        const double lo = box.mn[a];
        const double hi = box.mx[a];

        if (d == 0.0) {
            // 零方向分量：射线在该轴上坐标恒为 o，不做除法，杜绝 NaN。
            const double tol =
                scaleTol(eps, std::max(std::fabs(lo), std::fabs(hi)));
            if (o < lo - tol || o > hi + tol) {
                return result;  // 平行且不在 slab 内：必不相交
            }
            // 平行且在 slab 内：该轴不施加参数区间约束。
            continue;
        }

        // d 非零，除法安全。
        double t1 = (lo - o) / d;
        double t2 = (hi - o) / d;
        if (t1 > t2) std::swap(t1, t2);
        if (!std::isfinite(t1) || !std::isfinite(t2)) {
            return result;  // 防御：非有限输入不应到达这里
        }

        tEnter = std::max(tEnter, t1);
        tExit = std::min(tExit, t2);
        if (tEnter > tExit) return result;  // 区间提前变空（多数 miss 早退）
    }

    // 尺度相关容差作用在最终比较上，吸收相切附近的舍入误差。
    const double tolHit = scaleTol(eps, std::fabs(tExit));

    if (!(tExit >= -tolHit)) return result;  // 盒整体在起点后方

    const double tolLo = scaleTol(eps, std::fabs(tEnter));
    const bool startsInside = tEnter <= tolLo;

    if (startsInside) {
        // 起点在盒内（或恰在入射面上）：可见段从 t = 0 开始。
        result.hit = true;
        result.inside = pointInBox(ray.origin, box, eps);
        result.tEnter = 0.0;
        result.tExit = tExit > 0.0 ? tExit : 0.0;
        return result;
    }

    // 正向命中
    result.hit = true;
    result.inside = false;
    result.tEnter = tEnter;
    result.tExit = tExit;
    return result;
}

}  // namespace geo
