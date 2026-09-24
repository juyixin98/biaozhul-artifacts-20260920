// tf_math.hpp —— 刚体变换、四元数校验与时间插值（基于 Eigen，仅头文件）
#pragma once

#include <Eigen/Dense>
#include <Eigen/Geometry>
#include <stdexcept>
#include <vector>

namespace tfmath {

// 一个带时间戳的刚体变换样本。
// 约定：T = (p, q) 表示 *父坐标系 -> 子坐标系* 的变换，
//   p_child = R(q) * p_parent + t （即 ROS TF 中 transform 点的方向）。
struct Sample {
    double t = 0.0;          // 秒（double 时间戳，可任意纪元）
    Eigen::Vector3d p;       // 平移
    Eigen::Quaterniond q;    // 单位四元数（调用前已归一化）
};

// 四元数校验结果。
struct QuatCheck {
    bool valid = false;      // 是否可作为合法旋转
    bool normalized = false; // true: 模长接近 1（无需再归一化）
    double norm = 0.0;
    std::string reason;      // 非法原因
};

inline bool allFinite(const Eigen::Quaterniond& q) {
    return std::isfinite(q.w()) && std::isfinite(q.x()) &&
           std::isfinite(q.y()) && std::isfinite(q.z());
}

// 容差：与单位长度相差 1e-6 以内视为单位四元数；
// 模长过小视为无效（无法归一化）。
constexpr double kUnitTol = 1e-6;
constexpr double kMinNorm = 1e-9;

inline QuatCheck checkQuaternion(const Eigen::Quaterniond& q) {
    QuatCheck c;
    if (!allFinite(q)) {
        c.reason = "quaternion contains NaN/Inf";
        return c;
    }
    c.norm = q.norm();
    if (!std::isfinite(c.norm) || c.norm < kMinNorm) {
        c.reason = "quaternion norm too small to normalize";
        return c;
    }
    c.valid = true;
    c.normalized = std::abs(c.norm - 1.0) <= kUnitTol;
    if (!c.normalized) c.reason = "quaternion not unit length (will be normalized)";
    return c;
}

// 校验并归一化；非法时抛 std::invalid_argument。
inline Eigen::Quaterniond validatedQuaternion(Eigen::Quaterniond q) {
    QuatCheck c = checkQuaternion(q);
    if (!c.valid) throw std::invalid_argument(c.reason);
    if (!c.normalized) q.normalize();
    return q;
}

inline Eigen::Isometry3d toIsometry(const Eigen::Vector3d& p,
                                    const Eigen::Quaterniond& q) {
    Eigen::Isometry3d T = Eigen::Isometry3d::Identity();
    T.linear() = q.toRotationMatrix();
    T.translation() = p;
    return T;
}

inline Eigen::Isometry3d toIsometry(const Sample& s) {
    return toIsometry(s.p, s.q);
}

// 刚体变换乘法：a * b （先 b 后 a）。
inline Eigen::Isometry3d compose(const Eigen::Isometry3d& a,
                                 const Eigen::Isometry3d& b) {
    return a * b;
}

// 逆变换。
inline Eigen::Isometry3d inverse(const Eigen::Isometry3d& T) {
    return T.inverse();
}

// 逆采样（用于沿查询路径反向经过一条边）。
inline Sample invertSample(const Sample& s) {
    Sample r;
    r.t = s.t;
    Eigen::Isometry3d inv = toIsometry(s).inverse();
    r.p = inv.translation();
    r.q = Eigen::Quaterniond(inv.linear());
    r.q.normalize();
    return r;
}

// 把四元数规范为 w >= 0 的唯一表示，便于与解析结果逐分量比较。
inline Eigen::Quaterniond canonical(Eigen::Quaterniond q) {
    if (q.w() < 0.0) q.coeffs() = -q.coeffs();
    return q;
}

// 最短弧四元数插值（手写实现，不依赖 Q.slerp 的内部特殊分支）。
// 取 a、b 点积为非负的那一对，使球面插值始终走夹角 <= 90° 的短弧
// （180° 旋转差对应 d≈-1，翻转后等价于 theta≈0，天然避开转轴奇异）；
// theta 接近 0 时退化为归一化线性插值，避免 1/sin(theta) 除零。
inline Eigen::Quaterniond shortestArcSlerp(const Eigen::Quaterniond& aIn,
                                           const Eigen::Quaterniond& bIn,
                                           double u) {
    Eigen::Quaterniond a = aIn, b = bIn;
    double d = a.w() * b.w() + a.x() * b.x() + a.y() * b.y() + a.z() * b.z();
    if (d < 0.0) {                 // 取短弧
        b.coeffs() = -b.coeffs();
        d = -d;
    }
    d = std::min(1.0, std::max(-1.0, d));
    double theta = std::acos(d);   // 短弧翻转后 theta ∈ [0, pi/2]

    Eigen::Quaterniond r;
    if (theta < 1e-6) {
        // 几乎重合（含恰好 180° 被翻转为重合的情形）：归一化线性插值，
        // 避免 1/sin(theta) 除零。
        r.coeffs() = (1.0 - u) * a.coeffs() + u * b.coeffs();
    } else {
        double sinTheta = std::sin(theta);
        double wa = std::sin((1.0 - u) * theta) / sinTheta;
        double wb = std::sin(u * theta) / sinTheta;
        r.coeffs() = wa * a.coeffs() + wb * b.coeffs();
    }
    r.normalize();
    return r;
}

// 在两个样本之间做位姿插值：位置线性，旋转最短弧。time 必须位于 [a,b] 内。
inline Sample interpolate(const Sample& a, const Sample& b, double time) {
    if (a.t == b.t) return a;
    double u = (time - a.t) / (b.t - a.t);
    Sample r;
    r.t = time;
    r.p = (1.0 - u) * a.p + u * b.p;
    r.q = shortestArcSlerp(a.q, b.q, u);
    return r;
}

}  // namespace tfmath
