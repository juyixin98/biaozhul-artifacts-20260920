#include "geo.h"

#include <algorithm>
#include <cmath>

namespace geo {

namespace {
constexpr long double PI = 3.141592653589793238462643383279502884L;
constexpr long double DEG2RAD = PI / 180.0L;
constexpr long double LAT_SNAP_DEG = 1e-12L;
} // namespace

bool normalize(LatLon& p, std::string* error) {
    if (!std::isfinite(p.lat) || !std::isfinite(p.lon)) {
        if (error) *error = "latitude/longitude must be finite numbers";
        return false;
    }
    if (p.lat < -90.0L - LAT_SNAP_DEG || p.lat > 90.0L + LAT_SNAP_DEG) {
        if (error) *error = "latitude out of range [-90, 90]";
        return false;
    }
    if (p.lon < -180.0L || p.lon > 180.0L) {
        if (error) *error = "longitude out of range [-180, 180]";
        return false;
    }

    // 极点吸附：极微小越界视为极点；极点上经度无定义，统一为 0。
    if (p.lat >= 90.0L - LAT_SNAP_DEG) {
        p.lat = 90.0L;
        p.lon = 0.0L;
    } else if (p.lat <= -90.0L + LAT_SNAP_DEG) {
        p.lat = -90.0L;
        p.lon = 0.0L;
    } else if (p.lon == 180.0L) {
        p.lon = -180.0L; // 180 与 -180 是同一经线
    }
    return true;
}

long double centralAngle(const LatLon& a, const LatLon& b) {
    const long double lat1 = a.lat * DEG2RAD;
    const long double lat2 = b.lat * DEG2RAD;
    // 经度都规范化到 [-180, 180)，差值天然在 (-360, 360)；
    // 再规整到 [-180, 180]，反经线两侧的点取最短经差（例如 179 与 -179 → 2 度）。
    long double dlon_deg = b.lon - a.lon;
    while (dlon_deg > 180.0L) dlon_deg -= 360.0L;
    while (dlon_deg < -180.0L) dlon_deg += 360.0L;
    const long double dlon = dlon_deg * DEG2RAD;

    // 稳定的大圆地心角公式（矢量点积/叉积的 atan2 形式）：
    //   y = |n1 × n2|，x = n1 · n2（单位矢量），θ = atan2(y, x)。
    // 相比纯 haversine，它在重合点(θ≈0)与对跖点(θ≈π)两端都没有
    // 抵消病态：端点处分别退化为 atan2(0,1)=0 与 atan2(0,-1)=π。
    const long double s1 = std::sin(lat1), c1 = std::cos(lat1);
    const long double s2 = std::sin(lat2), c2 = std::cos(lat2);
    const long double sdl = std::sin(dlon), cdl = std::cos(dlon);
    const long double y2 = (c2 * sdl) * (c2 * sdl) +
                           (c1 * s2 - s1 * c2 * cdl) *
                               (c1 * s2 - s1 * c2 * cdl);
    const long double x = s1 * s2 + c1 * c2 * cdl;
    return std::atan2(std::sqrt(std::max(0.0L, y2)), x);
}

long double distanceM(const LatLon& a, const LatLon& b) {
    return EARTH_RADIUS_M * centralAngle(a, b);
}

CandidateBox makeCandidateBox(const LatLon& center, long double radius_m) {
    CandidateBox box{};
    const long double lat0 = center.lat * DEG2RAD;
    const long double lon0 = center.lon * DEG2RAD;
    const long double ang = radius_m / EARTH_RADIUS_M; // 角半径

    box.center_lon_rad = lon0;

    // 半径覆盖整个球：任意点都可能命中（对跖点恰好在边界上）。
    if (ang >= PI) {
        box.min_lat_rad = -PI / 2.0L;
        box.max_lat_rad = PI / 2.0L;
        box.full_longitude = true;
        return box;
    }

    box.min_lat_rad = std::max(-PI / 2.0L, lat0 - ang);
    box.max_lat_rad = std::min(PI / 2.0L, lat0 + ang);

    // 触及任一极点时，纬度圆退化为一点，经度无意义 → 经度全包含。
    if (box.min_lat_rad <= -PI / 2.0L || box.max_lat_rad >= PI / 2.0L) {
        box.full_longitude = true;
        return box;
    }

    // 经度半宽：球面小圆条件
    //   sin φ sin φ0 + cos φ cos φ0 cos Δλ >= cos δ
    // 要求包围盒对区间内所有纬度都成立，需取 RHS(φ) 的最小值。
    // RHS 在 sin φ* = sin φ0/cos δ 处取唯一极小值（未触极点时必在区间内），
    // 代入化简得 cos(half) = sqrt(cos²φ0 − sin²δ) / cos φ0。
    // 与“直接用 φ0 纬度上的经度宽度”相比，该值考虑了向高纬扩张，是安全上界；
    // 赤道上退化为 half = δ，直观正确。
    const long double sinD = std::sin(ang);
    long double inside = std::cos(lat0) * std::cos(lat0) - sinD * sinD;
    inside = std::max(0.0L, inside); // 未触极点时 >= 0，截断防浮点抖动
    long double ratio = std::sqrt(inside) / std::cos(lat0);
    ratio = std::max(-1.0L, std::min(1.0L, ratio));
    box.half_lon_rad = std::acos(ratio);
    return box;
}

bool CandidateBox::contains(const LatLon& p) const {
    const long double lat = p.lat * DEG2RAD;
    const long double lon = p.lon * DEG2RAD;
    // 1e-14 rad 角容差（约 0.06 µm）吸收“度数分别乘 π/180”带来的边界舍入，
    // 例如 15°·k + 30°·k 与 45°·k 末位不一致。最终裁决仍由精确球面距离决定。
    constexpr long double BOX_EPS = 1e-14L;
    if (lat < min_lat_rad - BOX_EPS || lat > max_lat_rad + BOX_EPS) return false;
    if (full_longitude) return true;

    long double dl = lon - center_lon_rad;
    while (dl > PI) dl -= 2.0L * PI;
    while (dl < -PI) dl += 2.0L * PI;
    return std::fabs(dl) <= half_lon_rad + BOX_EPS;
}

std::vector<Hit> rangeQuery(const LatLon& center,
                            long double radius_m,
                            const std::vector<LatLon>& points,
                            std::size_t* candidate_count) {
    const CandidateBox box = makeCandidateBox(center, radius_m);
    std::vector<Hit> hits;
    std::size_t candidates = 0;
    for (std::size_t i = 0; i < points.size(); ++i) {
        if (!box.contains(points[i])) continue;
        ++candidates;
        const long double d = distanceM(center, points[i]);
        // 边界含端点；1 纳米容差吸收包围盒外点在精确计算边缘的浮点抖动。
        if (d <= radius_m + 1e-9L) {
            hits.push_back(Hit{i, points[i], d});
        }
    }
    if (candidate_count) *candidate_count = candidates;
    return hits;
}

} // namespace geo
