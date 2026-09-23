// test_unit.cpp - 几何、简化、JSON 模块的 C++ 单元测试（自带 main）。
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <string>
#include <vector>

#include "../src/geometry.hpp"
#include "../src/json.hpp"
#include "../src/simplification.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void Check(bool cond, const std::string& name) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::printf("  [FAIL] %s\n", name.c_str());
  }
}

bool Near(double a, double b, double tol = 1e-12) {
  return std::fabs(a - b) <= tol;
}

std::vector<cps::Point> Pts(std::vector<std::pair<double, double>> v) {
  std::vector<cps::Point> out;
  for (auto [x, y] : v) out.push_back({x, y});
  return out;
}

std::vector<std::size_t> Indices(const cps::SimplifyResult& r) {
  return r.kept_indices;
}

// ---- PointToSegmentDistance ----
void TestPointToSegment() {
  using cps::Point;
  using cps::PointToSegmentDistance;
  const Point a{0, 0}, b{1, 0};

  Check(Near(PointToSegmentDistance({0.5, 1}, a, b), 1.0),
        "p2seg: perpendicular foot inside");
  Check(Near(PointToSegmentDistance({0, 0}, a, b), 0.0), "p2seg: at endpoint A");
  Check(Near(PointToSegmentDistance({1, 0}, a, b), 0.0), "p2seg: at endpoint B");
  Check(Near(PointToSegmentDistance({2, 1}, a, b), std::sqrt(2.0)),
        "p2seg: foot outside -> clamped to endpoint B");
  Check(Near(PointToSegmentDistance({-1, 1}, a, b), std::sqrt(2.0)),
        "p2seg: foot outside -> clamped to endpoint A");
  Check(Near(PointToSegmentDistance({0.5, 0}, a, b), 0.0),
        "p2seg: point on segment");
  // 零长度段：退化为点到点
  Check(Near(PointToSegmentDistance({3, 4}, {1, 1}, {1, 1}), std::sqrt(13.0)),
        "p2seg: zero-length segment -> point distance");
  double empty_dist = cps::PointToPolylineDistance({0, 0}, Pts({}));
  Check(std::isinf(empty_dist), "p2poly: empty polyline -> +inf");
  Check(Near(cps::PointToPolylineDistance({3, 4}, Pts({{1, 1}})),
             std::sqrt(13.0)),
        "p2poly: single point -> point distance");
}

// ---- 去重 ----
void TestDedup() {
  auto out = cps::RemoveConsecutiveDuplicates(
      Pts({{0, 0}, {0, 0}, {1, 1}, {1, 1}, {1, 1}, {2, 2}, {0, 0}}));
  Check(out.size() == 4, "dedup: collapses only consecutive repeats");
  Check(out[0] == cps::Point({0, 0}) && out[3] == cps::Point({0, 0}),
        "dedup: keeps non-consecutive repeat (foldback endpoints)");
  Check(cps::RemoveConsecutiveDuplicates({}).empty(), "dedup: empty -> empty");
  Check(cps::RemoveConsecutiveDuplicates(Pts({{5, 5}})).size() == 1,
        "dedup: single point preserved");
}

// ---- Douglas-Peucker 基本性质 ----
void TestDPBasic() {
  auto empty = cps::DouglasPeucker({}, 1.0);
  Check(empty.kept.empty(), "dp: empty -> empty");

  auto one = cps::DouglasPeucker(Pts({{7, 8}}), 1.0);
  Check(one.kept.size() == 1 && one.kept[0] == cps::Point({7, 8}),
        "dp: single point preserved");

  auto two = cps::DouglasPeucker(Pts({{0, 0}, {9, 9}}), 1.0);
  Check(two.kept.size() == 2, "dp: two points -> both kept");

  // 共线点 epsilon=0：去掉中间点，端点保留
  auto col = cps::DouglasPeucker(Pts({{0, 0}, {1, 0}, {2, 0}, {3, 0}}), 0.0);
  Check(Indices(col) == std::vector<std::size_t>({0, 3}),
        "dp: collinear, eps=0 -> endpoints only");

  // 共线但非等距，eps=0
  auto col2 = cps::DouglasPeucker(
      Pts({{0, 0}, {0.5, 0}, {2.5, 0}, {3, 0}}), 0.0);
  Check(Indices(col2) == std::vector<std::size_t>({0, 3}),
        "dp: collinear uneven, eps=0 -> endpoints only");

  // 锯齿 epsilon=0：所有偏离点保留
  auto zig = cps::DouglasPeucker(
      Pts({{0, 0}, {1, 1}, {2, 0}, {3, 1}, {4, 0}}), 0.0);
  Check(Indices(zig) == std::vector<std::size_t>({0, 1, 2, 3, 4}),
        "dp: zigzag, eps=0 -> all points kept");

  // 锯齿 epsilon=0.5：分裂后各子区间最大距离 0.5（落在端点上，严格相等），
  // 0.5 不大于 eps，故全保留——点到*线段*距离含端点钳制。
  auto zig2 = cps::DouglasPeucker(
      Pts({{0, 0}, {1, 1}, {2, 0}, {3, 1}, {4, 0}}), 0.5);
  Check(zig2.kept.size() == 5, "dp: zigzag, eps below split threshold -> all kept");

  // epsilon 恰好等于最大距离：严格 > 判据，中间点被丢弃
  auto zig3 = cps::DouglasPeucker(
      Pts({{0, 0}, {1, 1}, {2, 0}}), 1.0);
  Check(Indices(zig3) == std::vector<std::size_t>({0, 2}),
        "dp: distance == epsilon -> dropped (strict > rule)");
}

// ---- 回折：含非相邻重复点 ----
void TestDPFoldback() {
  // 对称 U 形回折：(0,0)->(2,0)->(2,-1)->(2,0)->(4,0)，
  // (2,0) 出现两次但不相邻；回折深度 1，两个回折点结构对称。
  auto fold = Pts({{0, 0}, {2, 0}, {2, -1}, {2, 0}, {4, 0}});

  // 根弦 0-4 上点 2 距离 1；分裂后两个回折点（1、3）距新弦
  // 均为 sqrt(0.8)=0.8944（点到*线段*距离，含端点钳制）。
  // eps=0.25：点 2 与回折点全部保留。
  auto r = cps::DouglasPeucker(fold, 0.25);
  Check(Indices(r) == std::vector<std::size_t>({0, 1, 2, 3, 4}),
        "dp foldback: small eps keeps fold-back points (segment metric)");
  Check(r.kept.front() == cps::Point({0, 0}) &&
            r.kept.back() == cps::Point({4, 0}),
        "dp foldback: endpoints retained");

  // eps=2：点 2 距离 1 <= 2，不分裂，仅留端点；单侧界仍成立。
  auto loose = cps::DouglasPeucker(fold, 2.0);
  Check(Indices(loose) == std::vector<std::size_t>({0, 4}),
        "dp foldback: large eps keeps only endpoints");
  double mx = 0.0;
  for (const auto& p : fold) {
    mx = std::max(mx, cps::PointToPolylineDistance(p, loose.kept));
  }
  Check(Near(mx, 1.0, 1e-12) && mx <= 2.0,
        "dp foldback: one-sided bound <= eps holds");

  // 同一输入重复运行，结果逐位一致（确定性）
  auto r2 = cps::DouglasPeucker(fold, 0.25);
  Check(Indices(r) == Indices(r2), "dp: deterministic across runs");

  // 阈值分层（对称 U 形）：
  // eps=0.95：回折点 0.8944 <= 0.95 被丢弃，保留 {0,2,4}
  Check(Indices(cps::DouglasPeucker(fold, 0.95)) ==
                std::vector<std::size_t>({0, 2, 4}),
        "dp foldback: eps between fold-dist and depth keeps detour only");
  // eps=0.85：0.8944 > 0.85，全部保留
  Check(Indices(cps::DouglasPeucker(fold, 0.85)) ==
                std::vector<std::size_t>({0, 1, 2, 3, 4}),
        "dp foldback: smaller eps keeps fold-back points");
  // eps=1.0：根弦最大距离恰为 1，严格 > 判据 -> 不分裂
  Check(Indices(cps::DouglasPeucker(fold, 1.0)) ==
                std::vector<std::size_t>({0, 4}),
        "dp foldback: eps == max distance, strict > rule");

  // 并列最远点：取最小下标
  // (0,0),(2,2),(2,-2),(4,0)：相对弦 (0,0)-(4,0)，两个中间点距离均为 2
  auto tie = cps::DouglasPeucker(
      Pts({{0, 0}, {2, 2}, {2, -2}, {4, 0}}), 1.0);
  Check(tie.kept_indices[1] == 1, "dp: ties resolve to lowest index");
}

// ---- 自交折线（蝴蝶结）----
void TestDPSelfIntersect() {
  auto cross = Pts({{0, 0}, {4, 4}, {4, 0}, {0, 4}});
  auto tight = cps::DouglasPeucker(cross, 1.0);
  Check(Indices(tight) == std::vector<std::size_t>({0, 1, 2, 3}),
        "dp self-intersect: small eps keeps every vertex");

  auto loose = cps::DouglasPeucker(cross, 5.0);
  Check(Indices(loose) == std::vector<std::size_t>({0, 3}),
        "dp self-intersect: large eps keeps endpoints");
  // 误差界（单侧）必须成立：点 (4,4) 到弦段 (0,0)-(0,4) 距离为 4
  double d = cps::PointToPolylineDistance({4, 4}, loose.kept);
  Check(Near(d, 4.0, 1e-12) && d <= 5.0,
        "dp self-intersect: one-sided bound <= eps still holds");
}

// ---- 零长输入与重复点（经主程序流水线，这里只验算法层）----
void TestDPZeroLengthSegments() {
  // 相邻重复点先去重再 DP；去重后三点共线，eps=0 仅留端点
  auto dedup = cps::RemoveConsecutiveDuplicates(
      Pts({{0, 0}, {0, 0}, {1, 0}, {1, 0}, {2, 0}}));
  Check(dedup.size() == 3, "pipeline: consecutive duplicates collapsed");
  auto r = cps::DouglasPeucker(dedup, 0.0);
  Check(Indices(r) == std::vector<std::size_t>({0, 2}),
        "pipeline: after dedup, collinear eps=0 -> endpoints only");

  // 全部为同一点：去重后仅 1 个点
  auto one = cps::RemoveConsecutiveDuplicates(
      Pts({{5, 5}, {5, 5}, {5, 5}}));
  Check(one.size() == 1, "pipeline: all identical -> one point");
}

// ---- DP 误差界不变量的穷举校验（小随机输入）----
void TestDPBoundInvariant() {
  // 对固定输入集合逐点检查 max dist(原始点 -> 简化折线) <= eps
  // （+ 适度舍入容差），覆盖多种折线形态。
  const double tol = 1e-10;
  std::vector<std::vector<cps::Point>> cases = {
      Pts({{0, 0}, {2, 2}, {2, -2}, {4, 0}}),
      Pts({{0, 0}, {2, 0}, {1, 0.5}, {2, 0}, {4, 0}}),
      Pts({{0, 0}, {4, 4}, {4, 0}, {0, 4}}),
      Pts({{0, 0}, {1, 1}, {2, 0}, {3, 1}, {4, 0}}),
      Pts({{-1.5, 2.25}, {0.3, -0.7}, {3.0, 3.0}, {3.0, 0.0}, {-2.0, -2.0},
           {5.0, 1.0}}),
  };
  for (double eps : {0.0, 0.1, 0.5, 1.0, 2.0, 10.0}) {
    for (const auto& cs : cases) {
      auto r = cps::DouglasPeucker(cs, eps);
      double mx = 0.0;
      for (const auto& p : cs) {
        mx = std::max(mx, cps::PointToPolylineDistance(p, r.kept));
      }
      Check(mx <= eps + tol,
            "dp invariant: one-sided bound for eps=" + std::to_string(eps));
    }
  }
}

// ---- JSON 往返 ----
void TestJson() {
  using cps::json::Value;
  std::string err;
  auto v = cps::json::Parse("{\"a\": 1, \"b\": [true, false, null, \"x\", -2.5e1]}",
                            &err);
  Check(v.is_object() && err.empty(), "json: parse object");
  Check(v.find("a")->as_number() == 1.0, "json: integer number");
  Check(v.find("b")->as_array()[4].as_number() == -25.0, "json: exponent");
  Check(v.find("b")->as_array()[2].is_null(), "json: null");

  auto bad1 = cps::json::Parse("{,}", &err);
  Check(!bad1.is_object() && !err.empty(), "json: reject malformed");
  auto bad2 = cps::json::Parse("[1,2] trailing", &err);
  Check(!bad2.is_object() && !err.empty(), "json: reject trailing input");
  auto bad3 = cps::json::Parse("[1,,3]", &err);
  Check(err.find("JSON") != std::string::npos || err.find("parse") != std::string::npos,
        "json: error message present");

  // 字符串字面量必须解析为字符串而非 bool（构造函数重载回归）
  Value lit("ok");
  Check(lit.is_string() && lit.as_string() == "ok",
        "json: const char* literal is string not bool");
  Value bv(true);
  Check(bv.is_bool() && bv.as_bool(), "json: explicit bool stays bool");

  // 往返：1/3 经 %.17g 序列化再解析
  Value numv(1.0 / 3.0);
  Value wrap = Value::Object();
  wrap.set("v", numv);
  std::string dumped = cps::json::Dump(wrap);
  std::string err2;
  Value back = cps::json::Parse(dumped, &err2);
  Check(err2.empty() && back.find("v")->as_number() == 1.0 / 3.0,
        "json: %.17g round-trips binary64 exactly");

  // 字符串转义
  Value s = Value::Object();
  s.set("k", Value(std::string("a\"b\\c\n")));
  std::string sd = cps::json::Dump(s);
  std::string err3;
  Value sb = cps::json::Parse(sd, &err3);
  Check(err3.empty() && sb.find("k")->as_string() == "a\"b\\c\n",
        "json: string escape round-trip");
}

}  // namespace

int main() {
  std::printf("Running unit tests...\n");
  TestPointToSegment();
  TestDedup();
  TestDPBasic();
  TestDPFoldback();
  TestDPSelfIntersect();
  TestDPZeroLengthSegments();
  TestDPBoundInvariant();
  TestJson();
  std::printf("%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
