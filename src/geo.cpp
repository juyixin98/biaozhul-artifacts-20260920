// geo.cpp — 球面距离与包围盒实现
#include "geo.hpp"

#include <algorithm>

namespace sph {

const char* errorMessage(GeoError e) {
    switch (e) {
        case GeoError::Ok: return "ok";
        case GeoError::LatOutOfRange: return "latitude out of range [-90, 90]";
        case GeoError::LonOutOfRange: return "longitude out of range [-180, 180]";
        case GeoError::RadiusNegative: return "search radius must be non-negative";
        case GeoError::NotFinite: return "value is not finite (NaN/Inf)";
    }
    return "unknown error";
}

long double wrapLongitude(long double lon) noexcept {
    // 先折到 [-180, 180)
    lon = std::fmod(lon, 360.0L);
    if (lon >= 180.0L) lon -= 360.0L;
    if (lon < -180.0L) lon += 360.0L;
    return lon;
}

GeoError validateLatLon(long double lat, long double lon) noexcept {
    if (!std::isfinite(lat) || !std::isfinite(lon)) return GeoError::NotFinite;
    if (lat < -90.0L || lat > 90.0L) return GeoError::LatOutOfRange;
    if (lon < -180.0L || lon > 180.0L) return GeoError::LonOutOfRange;
    return GeoError::Ok;
}

long double sphericalDistance(const GeoPoint& a, const GeoPoint& b) noexcept {
    // 同一纬度圈上的极点：经度无定义，(-90, x) 与 (-90, y) 是同一点。
    if (std::fabs(a.lat) > 90.0L - 1e-12L &&
        std::fabs(b.lat) > 90.0L - 1e-12L &&
        ((a.lat > 0 && b.lat > 0) || (a.lat < 0 && b.lat < 0))) {
        return 0.0L;
    }

    const long double phi1 = a.lat * DEG2RAD;
    const long double phi2 = b.lat * DEG2RAD;
    // Δλ 先在角度域归一化到 [-180,180)，保证跨反经线时取最短经度差，
    // 也避免 (180 与 -180) 被误判相差 360°。
    long double dlam = wrapLongitude(b.lon - a.lon) * DEG2RAD;

    // 用 sin/cos 的半角形式（haversine）：
    //   hav(x) = sin²(x/2)
    const long double sdphi = std::sin((phi2 - phi1) / 2.0L);
    const long double sdlon = std::sin(dlam / 2.0L);
    long double a_hav = sdphi * sdphi +
                        std::cos(phi1) * std::cos(phi2) * sdlon * sdlon;
    // 浮点误差可能使 a 略微超出 [0,1]；对跖点处 a→1，直接 acos(1-2a)
    // 在 a≈1 时会损失精度，因此统一用 atan2 求中心角（对跖点稳健）。
    a_hav = std::min(1.0L, std::max(0.0L, a_hav));
    const long double c = 2.0L * std::atan2(std::sqrt(a_hav),
                                            std::sqrt(1.0L - a_hav));
    return EARTH_RADIUS_M * c;
}

std::vector<BBox> BBox::split() const {
    if (!crossesAntimeridian()) return {*this};
    return {
        BBox{lat_min, lat_max, lon_min, 180.0L},
        BBox{lat_min, lat_max, -180.0L, lon_max},
    };
}

bool sphericalCapBBox(const GeoPoint& center, long double radius_m,
                      BBox& out) noexcept {
    if (!std::isfinite(radius_m) || radius_m < 0.0L) return false;

    const long double phi_c = center.lat * DEG2RAD;
    long double theta = radius_m / EARTH_RADIUS_M;  // 角半径（弧度）
    if (theta < 0) theta = 0;

    // 全球
    if (theta >= PI) {
        out = BBox{-90.0L, 90.0L, -180.0L, 180.0L};
        return true;
    }

    const long double lat_min_d = (phi_c - theta) * RAD2DEG;
    const long double lat_max_d = (phi_c + theta) * RAD2DEG;

    // 球面帽盖住极点：整个纬度范围跨过 ±90° ⇒ 经度无界
    bool coversPole = (lat_max_d >= 90.0L) || (lat_min_d <= -90.0L);
    if (coversPole) {
        out = BBox{
            std::max(-90.0L, lat_min_d),
            std::min(90.0L, lat_max_d),
            -180.0L, 180.0L};
        return true;
    }

    // 紧致经度跨度：Δλ = asin( sin θ / cos φ_c )
    // （球面帽在给定纬度处的半宽公式；比 θ 本身更紧）
    long double ratio = std::sin(theta) / std::cos(phi_c);
    ratio = std::min(1.0L, std::max(-1.0L, ratio));
    const long double half_lon = std::asin(ratio) * RAD2DEG;

    const long double lon_c = center.lon;
    long double lon_min_d = lon_c - half_lon;
    long double lon_max_d = lon_c + half_lon;

    // 越过 ±180 时按环展开（允许 lon 超出 [-180,180]）：
    //   lon_min > lon_max 即表示跨反经线，由 split()/contains 解释。
    if (lon_min_d < -180.0L) lon_min_d += 360.0L;
    if (lon_max_d > 180.0L) lon_max_d -= 360.0L;

    out = BBox{lat_min_d, lat_max_d, lon_min_d, lon_max_d};
    return true;
}

bool bboxContains(const BBox& box, const GeoPoint& p) noexcept {
    if (p.lat < box.lat_min || p.lat > box.lat_max) return false;
    // 全球经度退化情形：(-180, 180) 不是“跨越”，直接包含全部经度。
    if (box.lon_min <= -180.0L && box.lon_max >= 180.0L) return true;
    if (box.crossesAntimeridian()) {
        return p.lon >= box.lon_min || p.lon <= box.lon_max;
    }
    return p.lon >= box.lon_min && p.lon <= box.lon_max;
}

}  // namespace sph
