#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace geo {

// 固定地球半径（米）。球形模型常量，取常用平均半径 6371000 m。
// 注意：真实地球是椭球，本服务只做球面计算，不模拟 WGS84 椭球面距离。
inline constexpr long double EARTH_RADIUS_M = 6371000.0L;

struct LatLon {
    long double lat = 0.0L; // 纬度，度，[-90, 90]
    long double lon = 0.0L; // 经度，度，规范化到 [-180, 180)
};

struct Hit {
    std::size_t index = 0;
    LatLon point;
    long double distance_m = 0.0L;
};

// 校验并规范化坐标：
//  - lat/lon 必须是有限数；
//  - lat 允许范围 [-90, 90]，|lat| 与 90 的偏差小于 1e-12 度时吸附到极点；
//  - lon 允许范围 [-180, 180]，180 度规范化为 -180（两者是同一条经线）；
//  - 超出范围返回 false 并填写 error。
bool normalize(LatLon& p, std::string* error);

// 两点地心角（弧度，[0, pi]）。haversine + atan2 形式，
// 内部 long double 计算，h 截断到 [0,1]，对重合点与对跖点稳定。
long double centralAngle(const LatLon& a, const LatLon& b);

// 球面大圆距离（米），EARTH_RADIUS_M * centralAngle。
long double distanceM(const LatLon& a, const LatLon& b);

// 范围查询的候选包围盒（仅用于候选过滤，可能误纳，不会漏掉真正命中的点）。
// 纬度按角半径线性夹取；触及极点时经度全包含；
// 否则经度半宽按最靠近极点的纬度边保守估计，正确处理跨 ±180 度经线。
struct CandidateBox {
    long double min_lat_rad;
    long double max_lat_rad;
    long double half_lon_rad;
    bool full_longitude = false;
    long double center_lon_rad = 0.0L;

    bool contains(const LatLon& p) const;
};

CandidateBox makeCandidateBox(const LatLon& center, long double radius_m);

// 范围查询：返回 distance <= radius_m 的点（按输入索引顺序）。
// 先用包围盒做候选过滤，再用精确球面距离裁决。
// candidate_count（可选）输出通过包围盒的候选数，便于核对过滤行为。
std::vector<Hit> rangeQuery(const LatLon& center,
                            long double radius_m,
                            const std::vector<LatLon>& points,
                            std::size_t* candidate_count = nullptr);

} // namespace geo
