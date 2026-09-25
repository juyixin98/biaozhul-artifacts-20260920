// geo.hpp — 球面距离计算与球面帽包围盒（纯离线空间计算）
//
// 坐标系约定（详见 README.md「坐标系与模型」一节）：
//   - 输入输出均为 WGS84 参考椭球上的【地理经纬度】，角度制：
//       纬度 lat ∈ [-90, 90]（北正南负）
//       经度 lon ∈ [-180, 180)（东正西负；+180 与 -180 归一化为 -180）
//   - 但距离计算使用【球面模型】，地球半径固定为 IUGG 平均半径
//     6371008.8 m（不是 WGS84 椭球测地线）。
//   - 角度内部转换为弧度，long double 参与运算。
#pragma once

#include <array>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>

namespace sph {

// 固定地球半径（IUGG mean Earth radius R1 = 6371008.8 m），单位：米
inline constexpr long double EARTH_RADIUS_M = 6371008.8L;

inline constexpr long double PI = 3.141592653589793238462643383279502884L;
inline constexpr long double DEG2RAD = PI / 180.0L;
inline constexpr long double RAD2DEG = 180.0L / PI;

struct GeoPoint {
    long double lat = 0.0L;  // 纬度（度）
    long double lon = 0.0L;  // 经度（度）
};

// 经纬度合法性错误
enum class GeoError : std::uint8_t {
    Ok = 0,
    LatOutOfRange,   // 纬度不在 [-90, 90]
    LonOutOfRange,   // 经度不在 [-180, 180]
    RadiusNegative,  // 检索半径为负
    NotFinite,       // NaN / Inf
};

const char* errorMessage(GeoError e);

// 严格校验经纬度（不做容错归一化）。经度 +180/-180 均合法，
// 但内部点表示统一把 lon == 180 归一化为 -180。
GeoError validateLatLon(long double lat, long double lon) noexcept;

// 经度归一化到 [-180, 180)
long double wrapLongitude(long double lon) noexcept;

// 球面大圆距离（haversine 形式，atan2 求角），返回米。
//   d = R * atan2( sqrt(a), sqrt(1-a) )，a = hav(Δφ) + cosφ1·cosφ2·hav(Δλ)
// 退化情形：
//   - 两点重合（含同一极点的不同经度）→ 0
//   - 对跖点 → πR（半周长），acos 类方法在 ±1 处精度差，故不用 acos
long double sphericalDistance(const GeoPoint& a, const GeoPoint& b) noexcept;

// 轴对齐经纬度包围盒（角度制）。lon_min 可能大于 lon_max，表示盒子跨 ±180 经线。
// 跨 ±180 时 split() 给出两个普通盒子。
struct BBox {
    long double lat_min = 0, lat_max = 0;
    long double lon_min = 0, lon_max = 0;  // lon_min > lon_max ⇒ 跨反经线

    bool crossesAntimeridian() const noexcept { return lon_min > lon_max; }

    // 拆分为 1~2 个不跨反经线的盒子
    std::vector<BBox> split() const;
};

// 以 center 为圆心、半径 radius_m（米）的球面帽的紧致包围盒。
// 球面帽覆盖整个极（θ + |φc| ≥ 90°）时，经度方向退化为 [-180,180)。
// radius_m < 0 返回 false；radius 足够大（θ ≥ π）时盒子覆盖全球。
bool sphericalCapBBox(const GeoPoint& center, long double radius_m,
                      BBox& out) noexcept;

// 包围盒候选过滤（仅粗筛，不能作为最终距离判定）。
// 跨反经线的盒子在经度上按“环”处理。
bool bboxContains(const BBox& box, const GeoPoint& p) noexcept;

}  // namespace sph
