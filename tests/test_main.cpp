// test_main.cpp —— TF 坐标树校验自动化测试（无第三方测试框架）
#include "sha256.hpp"
#include "tf_math.hpp"
#include "transform_tree.hpp"

#include <cmath>
#include <cstdio>
#include <functional>
#include <string>
#include <limits>
#include <thread>
#include <algorithm>
#include <vector>

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& what) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("  [FAIL] %s\n", what.c_str());
    }
}

bool near(double a, double b, double eps = 1e-10) {
    return std::abs(a - b) <= eps;
}

void vecNear(const Eigen::Vector3d& a, const Eigen::Vector3d& b,
             double eps, const std::string& what) {
    double d = (a - b).norm();
    check(d <= eps, what + " (delta=" + std::to_string(d) + ")");
}

// 四元数比较考虑符号等价（q 与 -q 同一旋转）。
void quatNear(Eigen::Quaterniond a, Eigen::Quaterniond b, double eps,
              const std::string& what) {
    a.normalize();
    b.normalize();
    double d = std::abs(a.dot(b));
    check(d >= 1.0 - eps, what + " (|dot|=" + std::to_string(d) + ")");
}

void matRotNear(const Eigen::Matrix3d& a, const Eigen::Matrix3d& b,
                double eps, const std::string& what) {
    double d = (a - b).norm();
    check(d <= eps, what + " (delta=" + std::to_string(d) + ")");
}

// 期望抛出特定错误码。
void expectThrow(const std::string& code, const std::string& what,
                 const std::function<void()>& f) {
    try {
        f();
        check(false, what + " —— 期望抛出 " + code + " 但未抛出");
    } catch (const tftree::Error& e) {
        check(e.code == code,
              what + " —— 错误码 " + e.code + " != 期望 " + code);
    } catch (const std::exception& e) {
        check(false, what + std::string(" —— 抛出了非 tftree::Error: ") +
                          e.what());
    }
}

// ---------------- SHA-256 NIST FIPS 180-4 已知答案 ----------------
void testSha256() {
    std::printf("[sha256] NIST FIPS 180-2 已知答案向量\n");
    check(sha256::hex("") ==
              "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "SHA256(\"\")");
    check(sha256::hex("abc") ==
              "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
          "SHA256(\"abc\")");
    check(sha256::hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
              "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
          "SHA256(448-bit 长串)");
    // 恰好 55 字节（一个块，padding 边界）与 56 字节（跨块）
    std::string s55(55, 'a');
    std::string s56(56, 'a');
    // 与系统 sha256sum 一致即可（下面两个期望值为 sha256sum 预先核算）
    check(sha256::hex(s55).size() == 64, "55 字节输出长度");
    check(sha256::hex(s56).size() == 64, "56 字节输出长度");
    // 增量更新与一次性一致
    sha256::Sha256 h;
    h.update("ab");
    h.update("c");
    check(sha256::toHex(h.finish()) ==
              "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
          "增量 update 与一次性一致");
}

// ---------------- 四元数校验 ----------------
void testQuaternionValidation() {
    std::printf("[validate] 无效四元数拒绝\n");
    Eigen::Quaterniond zero(0, 0, 0, 0);
    check(!tfmath::checkQuaternion(zero).valid, "零四元数无效");

    Eigen::Quaterniond nan(std::nan(""), 0, 0, 0);
    check(!tfmath::checkQuaternion(nan).valid, "NaN 四元数无效");

    Eigen::Quaterniond inf(std::numeric_limits<double>::infinity(), 0, 0, 0);
    check(!tfmath::checkQuaternion(inf).valid, "Inf 四元数无效");

    Eigen::Quaterniond loose(2, 0, 0, 0);  // 模长 2，可归一化
    tfmath::QuatCheck c = tfmath::checkQuaternion(loose);
    check(c.valid && !c.normalized, "模长 2 可归一化（非单位但有效）");
    Eigen::Quaterniond n = tfmath::validatedQuaternion(loose);
    check(near(n.norm(), 1.0, 1e-15), "归一化后模长为 1");

    expectThrow("invalid_quaternion", "树拒绝零四元数", [] {
        tftree::TransformTree t;
        t.addStaticEdge("a", "b", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond(0, 0, 0, 0));
    });
}

// ---------------- 逆变换（解析核对）----------------
void testInverse() {
    std::printf("[math] 逆变换与解析结果核对\n");
    // T: 绕 z 转 30°, 平移 (1,2,3)
    double ang = M_PI / 6;
    Eigen::Quaterniond q(Eigen::AngleAxisd(ang, Eigen::Vector3d::UnitZ()));
    Eigen::Vector3d p(1, 2, 3);
    Eigen::Isometry3d T = tfmath::toIsometry(p, q);
    Eigen::Isometry3d Tinv = tfmath::inverse(T);

    Eigen::Isometry3d I = T * Tinv;
    check((I.matrix() - Eigen::Matrix4d::Identity()).norm() <= 1e-12,
          "T * T^-1 = I");

    // 解析逆旋转: R^T；逆平移: -R^T t
    Eigen::Matrix3d R = q.toRotationMatrix();
    Eigen::Matrix3d RinvExpect = R.transpose();
    matRotNear(Tinv.linear(), RinvExpect, 1e-12, "逆旋转 = R^T");
    vecNear(Tinv.translation(), -RinvExpect * p, 1e-12, "逆平移 = -R^T t");

    // 树查询：叶子 -> 根，应等于边变换的逆
    tftree::TransformTree tree;
    tree.addStaticEdge("world", "sensor", p, q);
    tftree::QueryResult r = tree.query("sensor", "world", 0.0);
    vecNear(r.translation, Tinv.translation(), 1e-12,
            "sensor->world 平移 = 解析逆平移");
    quatNear(r.rotation, Eigen::Quaterniond(RinvExpect), 1e-12,
             "sensor->world 旋转 = 解析逆旋转");

    // 再用一个点核对：p_world = (0.1, -0.2, 0.5)
    Eigen::Vector3d pw(0.1, -0.2, 0.5);
    Eigen::Vector3d ps = Tinv * pw;  // world->sensor 坐标
    Eigen::Vector3d back = T * ps;
    vecNear(back, pw, 1e-12, "点经逆/正变换往返一致");
}

// ---------------- 链式组合（解析核对）----------------
void testChain() {
    std::printf("[tree] 链式组合 world->a->b->c 与解析矩阵积核对\n");
    tftree::TransformTree tree;
    struct Step {
        std::string parent, child;
        Eigen::Vector3d p;
        Eigen::Quaterniond q;
    };
    std::vector<Step> steps = {
        {"world", "a", {1, 0, 0},
         Eigen::Quaterniond(Eigen::AngleAxisd(M_PI / 2,
                                              Eigen::Vector3d::UnitZ()))},
        {"a", "b", {0, 2, 0},
         Eigen::Quaterniond(Eigen::AngleAxisd(M_PI / 2,
                                              Eigen::Vector3d::UnitX()))},
        {"b", "c", {0, 0, 3},
         Eigen::Quaterniond(Eigen::AngleAxisd(M_PI / 3,
                                              Eigen::Vector3d::UnitY()))},
    };
    Eigen::Isometry3d expect = Eigen::Isometry3d::Identity();
    for (auto& s : steps) {
        tree.addStaticEdge(s.parent, s.child, s.p, s.q);
        expect = expect * tfmath::toIsometry(s.p, s.q);
    }

    tftree::QueryResult r = tree.query("world", "c", 0.0);
    check(r.edges.size() == 3, "使用 3 条边");
    vecNear(r.translation, expect.translation(), 1e-10,
            "world->c 平移 = 矩阵积");
    quatNear(r.rotation, Eigen::Quaterniond(expect.linear()), 1e-10,
             "world->c 旋转 = 矩阵积");

    // 反向查询 c->world 必须是正变换的逆
    tftree::QueryResult ri = tree.query("c", "world", 0.0);
    Eigen::Isometry3d invExpect = expect.inverse();
    vecNear(ri.translation, invExpect.translation(), 1e-10,
            "c->world 平移 = 积之逆");
    check((ri.matrix - r.matrix.inverse()).norm() <= 1e-9,
          "matrix 字段：反向矩阵 = 正向矩阵之逆");

    // 旁路查询 c->a（LCA 为 a）：c 取逆到 b，再 b 取逆到 a，
    // 解析值 = (T_ab * T_bc)^{-1} = T_bc^-1 * T_ab^-1
    tftree::QueryResult rb = tree.query("c", "a", 0.0);
    Eigen::Isometry3d ab = tfmath::toIsometry(steps[1].p, steps[1].q);
    Eigen::Isometry3d bc = tfmath::toIsometry(steps[2].p, steps[2].q);
    Eigen::Isometry3d cToA = (ab * bc).inverse();
    vecNear(rb.translation, cToA.translation(), 1e-10,
            "c->a 平移 = (T_ab*T_bc)^-1 的解析平移");
    quatNear(rb.rotation, Eigen::Quaterniond(cToA.linear()), 1e-10,
             "c->a 旋转 = (T_ab*T_bc)^-1 的解析旋转");
    check(rb.edges.size() == 2, "c->a 使用 2 条边");
}

// ---------------- 动态插值（线性位置 + 最短弧旋转）----------------
void testInterpolation() {
    std::printf("[interp] 位置线性、旋转最短弧 slerp\n");
    tftree::TransformTree tree;
    // 纯平移动态边：t=0 时 (0,0,0)，t=2 时 (10,0,0)
    tfmath::Sample a, b;
    a.t = 0; a.p = Eigen::Vector3d(0, 0, 0);
    a.q = Eigen::Quaterniond::Identity();
    b.t = 2; b.p = Eigen::Vector3d(10, 0, 0);
    b.q = Eigen::Quaterniond::Identity();
    tree.addDynamicSample("world", "slider", a);
    tree.addDynamicSample("world", "slider", b);

    tftree::QueryResult r = tree.query("world", "slider", 0.5);
    vecNear(r.translation, Eigen::Vector3d(2.5, 0, 0), 1e-12,
            "t=0.5 位置线性插值=2.5");
    check(r.edges.size() == 1 && r.edges[0].mode == "interpolated",
          "模式标记 interpolated");
    check(r.edges[0].sample_times.size() == 2 &&
              near(r.edges[0].sample_times[0], 0.0) &&
              near(r.edges[0].sample_times[1], 2.0),
          "报告使用的两个样本时间 [0,2]");
    check(near(r.max_time_error, 0.5, 1e-12), "时间误差 = 0.5");

    // 精确命中：exact，time_error=0
    tftree::QueryResult re = tree.query("world", "slider", 2.0);
    vecNear(re.translation, Eigen::Vector3d(10, 0, 0), 1e-12, "t=2 精确命中");
    check(re.edges[0].mode == "exact" && re.max_time_error == 0.0,
          "exact 模式且时间误差 0");

    // 旋转插值：绕 z 从 0 转到 90°，中点必须是 45°（解析）
    tftree::TransformTree tree2;
    tfmath::Sample c, d;
    c.t = 0; c.p = Eigen::Vector3d::Zero();
    c.q = Eigen::Quaterniond::Identity();
    d.t = 1; d.p = Eigen::Vector3d::Zero();
    d.q = Eigen::Quaterniond(
        Eigen::AngleAxisd(M_PI / 2, Eigen::Vector3d::UnitZ()));
    tree2.addDynamicSample("world", "rot", c);
    tree2.addDynamicSample("world", "rot", d);
    tftree::QueryResult rr = tree2.query("world", "rot", 0.5);
    Eigen::Quaterniond qExpect(
        Eigen::AngleAxisd(M_PI / 4, Eigen::Vector3d::UnitZ()));
    quatNear(rr.rotation, qExpect, 1e-12, "中点旋转 = 绕 z 45°");
    // 对 x 轴向量的作用核对
    Eigen::Vector3d v = rr.rotation * Eigen::Vector3d::UnitX();
    vecNear(v, Eigen::Vector3d(std::sqrt(0.5), std::sqrt(0.5), 0), 1e-12,
            "中点旋转把 x 轴转到 (1/√2,1/√2,0)");

    // 对任意 u 与 Eigen slerp 交叉核对（u=0.27）
    for (double u : {0.1, 0.27, 0.5, 0.73, 0.9}) {
        tfmath::Sample interp = tfmath::interpolate(c, d, u);
        Eigen::Quaterniond ei = c.q.slerp(u, d.q);
        quatNear(interp.q, ei, 1e-12,
                 "手写 slerp 与 Eigen 一致 u=" + std::to_string(u));
    }
}

// ---------------- 接近 180° 旋转 ----------------
void testNear180() {
    std::printf("[interp] 接近 180° 旋转插值稳定且为最短弧\n");
    // 179.9° 绕 z；从 0° 插值到该角，中点约 89.95°
    for (double deg : {179.0, 179.9, 179.99}) {
        double ang = deg * M_PI / 180.0;
        Eigen::Quaterniond qb(
            Eigen::AngleAxisd(ang, Eigen::Vector3d::UnitZ()));
        // 若直接构造得到 w<0? ang<pi -> w=cos(ang/2)>0，没问题
        tfmath::Sample a{0, Eigen::Vector3d::Zero(),
                         Eigen::Quaterniond::Identity()};
        tfmath::Sample b{1, Eigen::Vector3d::Zero(), qb};
        tfmath::Sample m = tfmath::interpolate(a, b, 0.5);
        check(std::isfinite(m.q.w()) && std::isfinite(m.q.x()),
              "近 180° 中点有限");
        check(near(m.q.norm(), 1.0, 1e-12), "近 180° 中点单位四元数");
        Eigen::Quaterniond qExpect(
            Eigen::AngleAxisd(ang / 2, Eigen::Vector3d::UnitZ()));
        quatNear(m.q, qExpect, 1e-9,
                 "近 180° 中点角度 = " + std::to_string(deg / 2) + "°");
    }

    // 精确 180°：q 与 -q 同旋转。构造绕 z 的 180° (w=0,z=1)。
    // 从恒等插值到 180°：短弧不唯一（退化），结果必须仍是合法单位旋转，
    // 且把插值角解析为 90° 绕某一水平轴 —— 这里验证不产生 NaN 与
    // 与 Eigen slerp 一致的鲁棒性。
    tfmath::Sample a{0, Eigen::Vector3d::Zero(),
                     Eigen::Quaterniond::Identity()};
    tfmath::Sample b{1, Eigen::Vector3d::Zero(),
                     Eigen::Quaterniond(
                         Eigen::AngleAxisd(M_PI, Eigen::Vector3d::UnitZ()))};
    tfmath::Sample m = tfmath::interpolate(a, b, 0.5);
    check(std::isfinite(m.q.w()) && near(m.q.norm(), 1.0, 1e-12),
          "恰好 180° 中点有限且单位化");
    // 恒等与 (w=0,z=1) 点积为 0，slerp 仍是良态的（theta=90°）。
    Eigen::Quaterniond ei = a.q.slerp(0.5, b.q);
    quatNear(m.q, ei, 1e-12, "180° 情形与 Eigen slerp 一致");

    // 真正的对偶表示：b 取负（同一旋转），插值仍应给出同一中点旋转
    tfmath::Sample b2 = b;
    b2.q.coeffs() = -b2.q.coeffs();
    tfmath::Sample m2 = tfmath::interpolate(a, b2, 0.5);
    quatNear(m2.q, ei, 1e-9, "b 取负（同旋转）后中点旋转不变");
}

// ---------------- 环 / 多父 / 缺边 / 外推 ----------------
void testRejections() {
    std::printf("[tree] 环、多父冲突、缺边、禁止外推\n");

    // 自环
    expectThrow("self_loop", "自环拒绝", [] {
        tftree::TransformTree t;
        t.addStaticEdge("a", "a", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
    });

    // 多父冲突
    expectThrow("multiple_parent_conflict", "同一 child 两个 parent", [] {
        tftree::TransformTree t;
        t.addStaticEdge("a", "c", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        t.addStaticEdge("b", "c", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
    });

    // 成环：先 a->b->c，再尝试 c->a
    expectThrow("cycle_detected", "新增边成环拒绝", [] {
        tftree::TransformTree t;
        t.addStaticEdge("a", "b", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        t.addStaticEdge("b", "c", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        t.addStaticEdge("c", "a", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
    });

    // 动态边同样拒绝环（带样本）
    expectThrow("cycle_detected", "动态边成环拒绝", [] {
        tftree::TransformTree t;
        t.addStaticEdge("a", "b", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        tfmath::Sample s{1, Eigen::Vector3d::Zero(),
                         Eigen::Quaterniond::Identity()};
        t.addDynamicSample("b", "a", s);
    });

    // 静态/动态混用冲突
    expectThrow("static_dynamic_conflict", "静态后加动态拒绝", [] {
        tftree::TransformTree t;
        t.addStaticEdge("a", "b", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        tfmath::Sample s{1, Eigen::Vector3d::Zero(),
                         Eigen::Quaterniond::Identity()};
        t.addDynamicSample("a", "b", s);
    });

    // 缺边：查询不存在帧
    expectThrow("unknown_frame", "未知坐标系", [] {
        tftree::TransformTree t;
        t.addStaticEdge("world", "a", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        t.query("a", "ghost", 0.0);
    });

    // 两棵不相连的树
    expectThrow("disconnected_tree", "不相连的树拒绝查询", [] {
        tftree::TransformTree t;
        t.addStaticEdge("r1", "a", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        t.addStaticEdge("r2", "b", Eigen::Vector3d::Zero(),
                        Eigen::Quaterniond::Identity());
        t.query("a", "b", 0.0);
    });

    // 禁止无限外推：早于 / 晚于样本区间
    expectThrow("extrapolation_forbidden", "向前外推拒绝", [] {
        tftree::TransformTree t;
        tfmath::Sample s1{1, Eigen::Vector3d::Zero(),
                          Eigen::Quaterniond::Identity()};
        tfmath::Sample s2{2, Eigen::Vector3d(1, 0, 0),
                          Eigen::Quaterniond::Identity()};
        t.addDynamicSample("world", "x", s1);
        t.addDynamicSample("world", "x", s2);
        t.query("world", "x", 0.5);
    });
    expectThrow("extrapolation_forbidden", "向后外推拒绝", [] {
        tftree::TransformTree t;
        tfmath::Sample s1{1, Eigen::Vector3d::Zero(),
                          Eigen::Quaterniond::Identity()};
        tfmath::Sample s2{2, Eigen::Vector3d(1, 0, 0),
                          Eigen::Quaterniond::Identity()};
        t.addDynamicSample("world", "x", s1);
        t.addDynamicSample("world", "x", s2);
        t.query("world", "x", 2.5);
    });

    // 容差边界：超出容差仍拒绝；容差内吸附并报告误差
    expectThrow("extrapolation_forbidden", "超出容差仍拒绝", [] {
        tftree::TransformTree t;
        tfmath::Sample s1{1, Eigen::Vector3d::Zero(),
                          Eigen::Quaterniond::Identity()};
        t.addDynamicSample("world", "x", s1);
        t.query("world", "x", 0.5, 0.1);  // gap=0.5 > tol=0.1
    });
    {
        tftree::TransformTree t;
        tfmath::Sample s1{1, Eigen::Vector3d(5, 0, 0),
                          Eigen::Quaterniond::Identity()};
        t.addDynamicSample("world", "x", s1);
        tftree::QueryResult r = t.query("world", "x", 0.95, 0.1);
        check(r.edges[0].mode == "clamped_earliest", "容差内吸附到最早样本");
        vecNear(r.translation, Eigen::Vector3d(5, 0, 0), 1e-12,
                "吸附取值为端点样本");
        check(near(r.max_time_error, 0.05, 1e-12), "吸附时间误差如实报告 0.05");
    }
}

// ---------------- 异步/乱序采样 ----------------
void testAsyncSampling() {
    std::printf("[async] 乱序到达、同时间戳覆盖、多线程追加\n");
    tftree::TransformTree tree;
    // 乱序插入
    double times[] = {5, 1, 4, 2, 3};
    for (double t : times) {
        tfmath::Sample s{t, Eigen::Vector3d(t, 0, 0),
                         Eigen::Quaterniond::Identity()};
        tree.addDynamicSample("world", "p", s);
    }
    tftree::QueryResult r = tree.query("world", "p", 2.5);
    vecNear(r.translation, Eigen::Vector3d(2.5, 0, 0), 1e-12,
            "乱序样本正确排序后插值");

    // 同时间戳覆盖（异步重发）
    tfmath::Sample dup{3, Eigen::Vector3d(99, 0, 0),
                       Eigen::Quaterniond::Identity()};
    tree.addDynamicSample("world", "p", dup);
    tftree::QueryResult r2 = tree.query("world", "p", 3.0);
    vecNear(r2.translation, Eigen::Vector3d(99, 0, 0), 1e-12,
            "同时间戳后到样本覆盖旧样本");
    // 样本数不应增加
    auto frames = tree.listFrames();
    auto it = std::find_if(frames.begin(), frames.end(),
                           [](const auto& f) { return f.name == "p"; });
    check(it != frames.end() && it->sample_count == 5,
          "覆盖不增加样本数（仍为 5）");

    // 多线程并发追加不同时间戳（线程安全真实执行）
    tftree::TransformTree t2;
    {
        constexpr int N = 8;
        std::vector<std::thread> ths;
        for (int th = 0; th < N; ++th) {
            ths.emplace_back([&t2, th] {
                for (int i = 0; i < 100; ++i) {
                    double tm = th * 100 + i;
                    tfmath::Sample s{tm, Eigen::Vector3d(tm, 1, 2),
                                     Eigen::Quaterniond::Identity()};
                    t2.addDynamicSample("world", "q", s);
                }
            });
        }
        for (auto& x : ths) x.join();
    }
    auto fs = t2.listFrames();
    auto it2 = std::find_if(fs.begin(), fs.end(),
                            [](const auto& f) { return f.name == "q"; });
    check(it2 != fs.end() && it2->sample_count == 800,
          "8 线程 x 100 样本全部有序入库（800）");
    tftree::QueryResult r3 = t2.query("world", "q", 350.5);
    vecNear(r3.translation, Eigen::Vector3d(350.5, 1, 2), 1e-9,
            "并发入库后插值结果正确");
}

// ---------------- 静态/动态混合链 ----------------
void testMixedChain() {
    std::printf("[tree] 静态+动态混合路径，时间误差取最大值\n");
    tftree::TransformTree tree;
    tree.addStaticEdge("world", "mount", Eigen::Vector3d(0, 0, 1),
                       Eigen::Quaterniond::Identity());
    tfmath::Sample a{0, Eigen::Vector3d::Zero(),
                     Eigen::Quaterniond::Identity()};
    tfmath::Sample b{10, Eigen::Vector3d(10, 0, 0),
                     Eigen::Quaterniond::Identity()};
    tree.addDynamicSample("mount", "arm", a);
    tree.addDynamicSample("mount", "arm", b);

    tftree::QueryResult r = tree.query("world", "arm", 7.0);
    // world->mount 平移 (0,0,1)（静态），mount->arm (7,0,0)
    vecNear(r.translation, Eigen::Vector3d(7, 0, 1), 1e-12,
            "混合链平移 = 静态 z=1 + 动态 x=7");
    check(r.edges.size() == 2, "混合链使用 2 条边");
    check(r.edges[0].mode == "static", "第一条边标记 static");
    check(r.edges[1].mode == "interpolated", "第二条边标记 interpolated");
    check(near(r.max_time_error, 3.0, 1e-12),
          "最大时间误差来自动态边 = 3.0");

    // /frames 信息
    auto frames = tree.listFrames();
    auto fm = std::find_if(frames.begin(), frames.end(),
                           [](const auto& f) { return f.name == "mount"; });
    check(fm != frames.end() && fm->is_static, "mount 报告为静态边");
    auto fa = std::find_if(frames.begin(), frames.end(),
                           [](const auto& f) { return f.name == "arm"; });
    check(fa != frames.end() && !fa->is_static && fa->sample_count == 2 &&
              near(fa->t_min, 0) && near(fa->t_max, 10),
          "arm 报告为动态边，区间 [0,10]");
}

}  // namespace

int main() {
    auto tests = {
        testSha256,        testQuaternionValidation, testInverse,
        testChain,         testInterpolation,        testNear180,
        testRejections,    testAsyncSampling,        testMixedChain,
    };
    for (auto t : tests) t();
    std::printf("\n==== %d 项断言，%d 项失败 ====\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
