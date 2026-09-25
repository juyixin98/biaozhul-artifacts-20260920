// test_units.cpp — 扫描线结果 vs 小网格逐格参考：结构化用例与随机 fuzz
#include <cstdio>
#include <cstdint>
#include <random>
#include <string>
#include <vector>

#include "../src/geometry.hpp"
#include "brute_force.hpp"

using namespace rectunion;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& name) {
    ++g_checks;
    if (cond) {
        std::printf("  [PASS] %s\n", name.c_str());
    } else {
        ++g_failures;
        std::printf("  [FAIL] %s\n", name.c_str());
    }
}

bool equalsBrute(const std::vector<Rect>& rects, const std::string& name,
                 bool verbose = true) {
    Metrics m = unionMetrics(rects);
    test::BruteMetrics b = test::bruteForce(rects);
    bool ok = m.area == b.area && m.perimeter == b.perimeter;
    if (verbose) {
        check(ok, name + "  (扫描线 area=" + toString(m.area) +
                      " perim=" + toString(m.perimeter) +
                      " | 逐格 area=" + std::to_string(b.area) +
                      " perim=" + std::to_string(b.perimeter) + ")");
    }
    return ok;
}

bool expect(const std::vector<Rect>& rects, u128 wantArea, u128 wantPerim,
            const std::string& name) {
    Metrics m = unionMetrics(rects);
    bool ok = m.area == wantArea && m.perimeter == wantPerim;
    check(ok, name + "  (area=" + toString(m.area) + "/" + toString(wantArea) +
                  " perim=" + toString(m.perimeter) + "/" + toString(wantPerim) + ")");
    return ok;
}

// 结构化用例：覆盖嵌套、相邻、零面积、同 x 多事件、点接触等退化情形。
void structuredCases() {
    std::printf("== 结构化用例（小网格逐格参考） ==\n");

    equalsBrute({}, "空输入");
    equalsBrute({{0, 0, 0, 0}, {1, 1, 1, 4}, {2, 3, 5, 3}},
                "仅零面积矩形（点/竖线/横线）");

    equalsBrute({{0, 0, 2, 3}}, "单个矩形 2×3（area=6 perim=10）");

    // 横向相邻，共享竖边 x=2：公共边不计
    equalsBrute({{0, 0, 2, 2}, {2, 0, 4, 2}},
                "横向相邻（合成 4×2，area=8 perim=12）");
    // 纵向相邻，共享横边 y=2
    equalsBrute({{0, 0, 2, 2}, {0, 2, 2, 4}},
                "纵向相邻（合成 2×4，area=8 perim=12）");
    // 三段横向并排，两个同 x 贴合面
    equalsBrute({{0, 0, 1, 1}, {1, 0, 2, 1}, {2, 0, 3, 1}},
                "三段并排（合成 3×1，含同 x 多事件）");

    // 部分重叠
    equalsBrute({{0, 0, 3, 3}, {2, 2, 5, 5}},
                "对角重叠 3×3 与 3×3 重叠 1×1（area=17 perim=20）");

    // 嵌套：内矩形完全在外矩形内部，内矩形四条边都不是并集边界。
    // 注意：一个矩形完全包含另一个时，并集仍是实心的（并不会产生洞）。
    equalsBrute({{0, 0, 6, 6}, {2, 2, 4, 4}},
                "完全嵌套（area=36 perim=24，内边不计）");

    // 同样验证 8×8 包含 4×4：面积 64、周长仅外框 32
    equalsBrute({{0, 0, 8, 8}, {2, 2, 6, 6}},
                "更大外框包含内矩形（area=64 perim=32）");

    // 真·带洞环：四块 U 形边框围出中心空洞，洞的周长计入并集周长
    equalsBrute({
        {0, 0, 8, 2},   // 下
        {0, 6, 8, 8},   // 上
        {0, 2, 2, 6},   // 左
        {6, 2, 8, 6},   // 右
    }, "环形边框带洞（外 8×8 挖去中心 4×4：area=48 perim=48）");

    // 仅角点接触（半开语义下两个闭包只交于一点，面积与周长互不影响）
    equalsBrute({{0, 0, 2, 2}, {2, 2, 4, 4}},
                "仅角点接触（area=8 perim=16）");

    // 完全重复
    equalsBrute({{1, 1, 4, 4}, {1, 1, 4, 4}},
                "完全重复（area=9 perim=12）");

    // 混合：嵌套 + 相邻 + 零面积 + 重复 + 同 x 多事件
    equalsBrute({
        {0, 0, 6, 4},    // 底条
        {1, 4, 3, 7},    // 左塔，底贴底条上沿
        {3, 4, 6, 7},    // 右塔，与左塔在 x=3 贴合、底贴底条
        {2, 5, 4, 6},    // 跨塔小矩形，分别与两塔重叠
        {0, 0, 6, 4},    // 重复底条
        {4, 4, 4, 7},    // 零宽竖线
        {1, 4, 3, 4},    // 零高横线
    }, "混合：嵌套+相邻+零面积+重复+同x多事件");

    // 负坐标
    equalsBrute({{-3, -3, 0, 0}, {0, 0, 3, 3}},
                "负坐标 + 角点接触");
    equalsBrute({{-4, -1, 4, 1}, {-1, -4, 1, 4}},
                "十字形（负坐标，area=28 perim=32）");

    // 同 x 处既有左缘又有右缘（+1/-1 混合），且多个事件同 x
    equalsBrute({
        {0, 0, 2, 2},    // 于 x=2 关闭
        {2, 0, 4, 2},    // 于 x=2 开启（相邻贴合）
        {2, 3, 4, 5},    // 于 x=2 开启（与下方分离的另一事件，同 x 多事件）
        {4, 3, 6, 5},    // 于 x=4 关闭/开启混合
    }, "同一 x 混合开/关事件多个");
}

// 大数：无法用逐格参考，直接验证面积/周长的 128 位运算与负值跨度。
void largeCases() {
    std::printf("== 大范围/精度用例（int64 边界，__int128 内部运算） ==\n");

    // 跨度 2^64-1：[INT64_MIN, INT64_MAX) 的半开边长为 2^64-1。
    Rect giant{INT64_MIN, INT64_MIN, INT64_MAX, INT64_MAX};
    u128 span = (u128(1) << 64) - 1;
    expect({giant}, span * span, u128(4) * span,
           "最大单矩形（跨度 2^64-1，面积 (2^64-1)^2，周长 4×跨度）");

    // 两个最大半幅矩形纵向贴合：贴合边不重复计数
    Rect g1{INT64_MIN, INT64_MIN, INT64_MAX, 0};
    Rect g2{INT64_MIN, 0, INT64_MAX, INT64_MAX};
    Metrics mm = unionMetrics({g1, g2});
    check(mm.area == span * span && mm.perimeter == u128(4) * span,
          "两个最大半幅矩形纵向贴合（公共横边不计，周长仅外框 4×跨度）");

    // 小矩形放在极端坐标处，检查加法不溢出 64 位
    equalsBrute({{-1, -1, 1, 1}}, "中心小矩形（手工网格）");
}

// 随机 fuzz：在小网格上生成随机整数矩形（含退化、重复），逐格参考对比。
void fuzzCases() {
    std::printf("== 随机 fuzz（20000 组，逐格参考） ==\n");
    std::mt19937 rng(20260924u);
    const int GRID = 8;
    int trials = 20000;
    int failed = 0;
    std::string firstFailure;

    for (int t = 0; t < trials; ++t) {
        int n = static_cast<int>(rng() % 11); // 0..10 个
        std::vector<Rect> rects;
        rects.reserve(n);
        for (int k = 0; k < n; ++k) {
            // 偶尔制造退化矩形
            int a = static_cast<int>(rng() % (GRID + 1));
            int b = static_cast<int>(rng() % (GRID + 1));
            int c = static_cast<int>(rng() % (GRID + 1));
            int d = static_cast<int>(rng() % (GRID + 1));
            Rect r{
                static_cast<long long>(std::min(a, b)),
                static_cast<long long>(std::min(c, d)),
                static_cast<long long>(std::max(a, b)),
                static_cast<long long>(std::max(c, d)),
            };
            // 10% 概率重复加入上一个矩形
            if (!rects.empty() && rng() % 10 == 0) r = rects.back();
            rects.push_back(r);
        }
        Metrics m = unionMetrics(rects);
        test::BruteMetrics bf = test::bruteForce(rects);
        if (m.area != bf.area || m.perimeter != bf.perimeter) {
            ++failed;
            if (firstFailure.empty()) {
                firstFailure = "area " + toString(m.area) + " vs " +
                               std::to_string(bf.area) + "; perim " +
                               toString(m.perimeter) + " vs " +
                               std::to_string(bf.perimeter) + " | rects:";
                for (const Rect& r : rects)
                    firstFailure += " (" + std::to_string(r.x1) + "," +
                                    std::to_string(r.y1) + ")-(" +
                                    std::to_string(r.x2) + "," +
                                    std::to_string(r.y2) + ")";
            }
        }
    }
    check(failed == 0,
          "fuzz " + std::to_string(trials) + " 组全部一致" +
              (failed ? ("；首个分歧：" + firstFailure) : ""));
}

} // namespace

int main() {
    structuredCases();
    largeCases();
    fuzzCases();
    std::printf("\n共 %d 项检查，%d 项失败。\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
