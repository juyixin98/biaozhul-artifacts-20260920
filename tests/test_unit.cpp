// Unit tests (no framework beyond a tiny macro harness).
// Build via CMake target `pgo_tests`; `ctest --test-dir build` runs them.
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <unistd.h>
#include <vector>

#include <nlohmann/json.hpp>

#include "canonical.h"
#include "crypto.h"
#include "db.h"
#include "graph.h"
#include "optimizer.h"
#include "report.h"
#include "util.h"

using json = nlohmann::json;
using namespace pgo;

namespace {

int g_checks = 0;
int g_failures = 0;
std::vector<std::pair<std::string, std::function<void()>>> g_tests;

#define CHECK(cond)                                                       \
  do {                                                                    \
    ++g_checks;                                                           \
    if (!(cond)) {                                                        \
      ++g_failures;                                                       \
      std::printf("    CHECK failed: %s (%s:%d)\n", #cond, __FILE__,      \
                  __LINE__);                                              \
    }                                                                     \
  } while (0)

#define CHECK_NEAR(a, b, tol)                                             \
  do {                                                                    \
    ++g_checks;                                                           \
    double va_ = static_cast<double>(a), vb_ = static_cast<double>(b);    \
    if (std::abs(va_ - vb_) > (tol)) {                                    \
      ++g_failures;                                                       \
      std::printf("    CHECK_NEAR failed: %s=%.12g vs %s=%.12g\n", #a,    \
                  va_, #b, vb_);                                          \
    }                                                                     \
  } while (0)

#define TEST(name) \
  static void test_##name(); \
  static bool reg_##name = \
      (g_tests.emplace_back(#name, test_##name), true); \
  static void test_##name()

TEST(wrap_normalizes_to_expected_interval) {
  CHECK(NormalizeAngle(0.0) == 0.0);
  // +/-3pi coincide with -pi in (-pi, pi]; fmod maps both to -pi.
  CHECK_NEAR(NormalizeAngle(3.0 * kPi), -kPi, 1e-12);
  CHECK_NEAR(NormalizeAngle(-3.0 * kPi), -kPi, 1e-12);
  CHECK_NEAR(NormalizeAngle(7.0), 7.0 - 2 * kPi, 1e-12);
  CHECK(NormalizeAngle(1e18) <= kPi && NormalizeAngle(1e18) >= -kPi);
  CHECK(NormalizeAngle(-1e18) <= kPi && NormalizeAngle(-1e18) >= -kPi);
}

TEST(se2_compose_inverse_roundtrip) {
  Pose a{1.0, 2.0, 1.2};
  Pose b{0.5, -0.3, -2.0};
  Pose c = Compose(a, b);
  Pose rec = Compose(c, Inverse(b));
  CHECK_NEAR(rec.x, a.x, 1e-12);
  CHECK_NEAR(rec.y, a.y, 1e-12);
  CHECK_NEAR(rec.theta, a.theta, 1e-12);
  Pose id = Compose(a, Inverse(a));
  CHECK_NEAR(id.x, 0, 1e-12);
  CHECK_NEAR(id.y, 0, 1e-12);
  CHECK_NEAR(id.theta, 0, 1e-12);
}

TEST(consistent_edge_has_zero_residual) {
  Pose from{0, 0, 0.5}, z{1, 0, 0.3};
  Pose to = Compose(from, z);
  auto e = EdgeResidual(from, to, z);
  CHECK_NEAR(e[0], 0, 1e-10);
  CHECK_NEAR(e[1], 0, 1e-10);
  CHECK_NEAR(e[2], 0, 1e-10);
}

TEST(heading_residual_wraps_at_branch_cut) {
  // theta from=3.1, true to=wrap(3.2) ~ -3.08, small forward turn.
  Pose from{0, 0, 3.1};
  Pose z{1.0, 0.0, 0.1};
  Pose to{1.0, 0.0, NormalizeAngle(3.2)};
  auto e = EdgeResidual(from, to, z);
  CHECK_NEAR(e[2], 0, 1e-9);
  CHECK(std::abs(e[2]) <= kPi);
}

// ---- graph loading / validation ----
std::string BaseGraphJson() {
  return R"({
    "protocol":"pgo-input/1.0","version":"1.0","graph_version":"unit-v1",
    "options":{"anchor_mode":"single"},
    "nodes":[
      {"id":"a","init":[0,0,0],"fixed":true},
      {"id":"b","init":[1,0,0]}
    ],
    "edges":[
      {"id":"ab","from":"a","to":"b","measurement":[1,0,0],
       "information":[10,0,0,0,10,0,0,0,10]}
    ]
  })";
}

TEST(non_spd_information_rejected) {
  std::string j = BaseGraphJson();
  j.replace(j.find("[10,0,0,0,10,0,0,0,10]"),
            std::string("[10,0,0,0,10,0,0,0,10]").size(),
            "[10,0,0,0,-1,0,0,0,10]");
  auto lr = LoadGraphFromJson(j);
  CHECK(!lr.ok);
  bool found = false;
  for (auto& i : lr.validation.issues)
    found |= i.code == "information.not_positive_definite";
  CHECK(found);
}

TEST(asymmetric_information_rejected) {
  std::string j = BaseGraphJson();
  j.replace(j.find("[10,0,0,0,10,0,0,0,10]"),
            std::string("[10,0,0,0,10,0,0,0,10]").size(),
            "[10,2,0,0,10,0,0,0,10]");
  auto lr = LoadGraphFromJson(j);
  CHECK(!lr.ok);
  bool found = false;
  for (auto& i : lr.validation.issues)
    found |= i.code == "information.asymmetric";
  CHECK(found);
}

TEST(ill_conditioned_spd_warns_but_accepted) {
  std::string j = BaseGraphJson();
  j.replace(j.find("[10,0,0,0,10,0,0,0,10]"),
            std::string("[10,0,0,0,10,0,0,0,10]").size(),
            "[1e15,0,0,0,1e15,0,0,0,1]");
  auto lr = LoadGraphFromJson(j);
  CHECK(lr.ok);
  bool warned = false;
  for (auto& i : lr.validation.issues)
    warned |= i.code == "information.ill_conditioned";
  CHECK(warned);
}

TEST(disconnected_single_mode_fails_per_component_ok) {
  std::string j = R"({
    "protocol":"pgo-input/1.0","version":"1.0","graph_version":"disc-v1",
    "options":{"anchor_mode":"single"},
    "nodes":[{"id":"a","init":[0,0,0],"fixed":true},{"id":"b","init":[1,0,0]},
             {"id":"c","init":[9,9,0],"fixed":true},{"id":"d","init":[10,9,0]}],
    "edges":[
      {"id":"ab","from":"a","to":"b","measurement":[1,0,0],
       "information":[10,0,0,0,10,0,0,0,10]},
      {"id":"cd","from":"c","to":"d","measurement":[1,0,0],
       "information":[10,0,0,0,10,0,0,0,10]}
    ]})";
  auto single = LoadGraphFromJson(j);
  CHECK(!single.ok);
  bool disc = false;
  for (auto& i : single.validation.issues)
    disc |= i.code == "graph.disconnected";
  CHECK(disc);

  std::string j2 = j;
  j2.replace(j2.find("\"single\""), 8, "\"per_component\"");
  auto per = LoadGraphFromJson(j2);
  CHECK(per.ok);
  CHECK(per.validation.components.size() == 2);
  ValidationReport ar;
  auto anchors = SelectAnchors(per.graph, per.validation.components, &ar);
  CHECK(anchors.size() == 2);
  CHECK(anchors[0] == "a");
  CHECK(anchors[1] == "c");
}

// ---- canonical + crypto ----
TEST(canonical_json_is_key_sorted_and_stable) {
  json a = {{"z", 1}, {"a", json{{"y", 2}, {"x", 1}}}};
  json b = {{"a", json{{"x", 1}, {"y", 2}}}, {"z", 1}};
  CHECK(CanonicalJson(a) == CanonicalJson(b));
  // child key indent is 2 spaces per level
  CHECK(CanonicalJson(a).find("{\n  \"a\"") != std::string::npos);
  CHECK(CanonicalJson(a).find("{\n    \"x\"") != std::string::npos);
}

TEST(sha256_known_vectors) {
  CHECK(Sha256Hex("") ==
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  CHECK(Sha256Hex("abc") ==
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
}

TEST(hmac_sha256_rfc4231_case1) {
  std::string key(20, '\x0b');
  std::string mac, err;
  CHECK(HmacSha256Hex(key, "Hi There", &mac, &err));
  CHECK(mac == "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
  CHECK(!HmacSha256Hex("", "x", &mac, &err));
}

TEST(robust_rho_downweights_outliers) {
  double q = RobustRho("none", 1, 4.0);
  double h = RobustRho("huber", 1, 4.0);
  double c = RobustRho("cauchy", 1, 4.0);
  CHECK_NEAR(q, 4.0, 1e-12);
  CHECK(h < 4.0);
  CHECK(c < h);
  CHECK_NEAR(RobustRho("huber", 1, 0.25), 0.25, 1e-12);  // quadratic inlier
}

// ---- optimization ----
// Build a drifted square: n nodes around a square perimeter, every edge
// measures unit advance with 90deg turns at the corners plus a noisy
// closing edge. Odometry is physically consistent (same square), only the
// initial guesses carry the drift.
LoadResult SquareGraph() {
  json nodes = json::array();
  const int side = 4;             // 4 unit segments per side
  const int n = 4 * side;         // n0..n15, n0 == corner anchor
  int corner[3] = {side, 2 * side, 3 * side};
  double truth_x = 0, truth_y = 0, truth_t = 0;
  for (int i = 0; i < n; ++i) {
    // accumulate truth pose
    double tx = truth_x, ty = truth_y, tt = truth_t;
    double initx = tx + 0.12 * i;
    double inity = ty + 0.01 * i * i;
    double initt = NormalizeAngle(tt + 0.015 * i);
    nodes.push_back({{"id", "n" + std::to_string(i)},
                     {"init", {initx, inity, initt}},
                     {"fixed", i == 0}});
    // advance truth to the next node
    if (i + 1 == corner[0] || i + 1 == corner[1] || i + 1 == corner[2])
      truth_t += kPi / 2;
    truth_x += std::cos(truth_t);
    truth_y += std::sin(truth_t);
    (void)tt;
  }
  json edges = json::array();
  double t = 0;
  for (int i = 0; i < n - 1; ++i) {
    double mt = 0;
    if (i + 1 == corner[0] || i + 1 == corner[1] || i + 1 == corner[2]) {
      mt = kPi / 2;
      t += kPi / 2;
    }
    edges.push_back({{"id", "e" + std::to_string(i)},
                     {"from", "n" + std::to_string(i)},
                     {"to", "n" + std::to_string(i + 1)},
                     {"measurement", {1.0, 0.0, mt}},
                     {"information", {40, 0, 0, 0, 40, 0, 0, 0, 60}}});
  }
  // closing edge n15 -> n0: unit advance with final +90deg turn
  edges.push_back({{"id", "loop"},
                   {"from", "n" + std::to_string(n - 1)},
                   {"to", "n0"},
                   {"measurement", {1.0, 0.0, kPi / 2}},
                   {"information", {40, 0, 0, 0, 40, 0, 0, 0, 60}}});
  (void)t;
  json doc = {{"protocol", kInputProtocol},
              {"version", "1.0"},
              {"graph_version", "square-unit-v1"},
              {"options",
               {{"anchor_mode", "single"},
                {"loss_type", "none"},
                {"max_iterations", 100}}},
              {"nodes", nodes},
              {"edges", edges}};
  return LoadGraphFromJson(CanonicalJson(doc));
}

TEST(square_converges_and_drift_removed) {
  auto lr = SquareGraph();
  CHECK(lr.ok);
  ValidationReport ar;
  auto anchors = SelectAnchors(lr.graph, lr.validation.components, &ar);
  CHECK(anchors.size() == 1);
  auto o = Optimize(lr.graph, anchors, lr.graph.options);
  CHECK(o.converged);
  CHECK(o.final.weighted_cost < o.initial.weighted_cost * 1e-4);
  CHECK(o.final.weighted_cost < 1e-6);
  // corners: n4 at (4,0), n8 at (4,4), n12 at (0,4)
  const auto& find_pose = [&](const std::string& id) -> const Pose& {
    for (size_t i = 0; i < o.node_order.size(); ++i)
      if (o.node_order[i] == id) return o.optimized[i];
    return o.optimized[0];
  };
  CHECK_NEAR(find_pose("n4").x, 4.0, 0.02);
  CHECK_NEAR(find_pose("n4").y, 0.0, 0.02);
  CHECK_NEAR(find_pose("n8").x, 4.0, 0.02);
  CHECK_NEAR(find_pose("n8").y, 4.0, 0.02);
  CHECK_NEAR(find_pose("n12").x, 0.0, 0.02);
  CHECK_NEAR(find_pose("n12").y, 4.0, 0.02);
}

TEST(huber_protects_good_edges_from_false_loop) {
  // z is held at (2,0) by strong correct edges; the false "bad" edge
  // c(2,0)->z(2,0) claims a 5m translation — an irreconcilable loop.
  const char* j = R"({
   "protocol":"pgo-input/1.0","version":"1.0","graph_version":"bad-unit",
   "options":{"anchor_mode":"single","loss_type":"huber","loss_param":1.0},
   "nodes":[{"id":"a","init":[0,0,0],"fixed":true},{"id":"b","init":[1,0,0]},
            {"id":"c","init":[2,0,0]},{"id":"z","init":[2.05,0.08,0.03]}],
   "edges":[
     {"id":"ab","from":"a","to":"b","measurement":[1,0,0],
      "information":[50,0,0,0,50,0,0,0,80]},
     {"id":"bc","from":"b","to":"c","measurement":[1,0,0],
      "information":[50,0,0,0,50,0,0,0,80]},
     {"id":"az","from":"a","to":"z","measurement":[2,0,0],
      "information":[60,0,0,0,60,0,0,0,90]},
     {"id":"bz","from":"b","to":"z","measurement":[1,0,0],
      "information":[60,0,0,0,60,0,0,0,90]},
     {"id":"bad","from":"c","to":"z","measurement":[5,0,0],
      "information":[50,0,0,0,50,0,0,0,80]}
   ]})";
  auto lr = LoadGraphFromJson(j);
  CHECK(lr.ok);
  auto o = Optimize(lr.graph, {"a"}, lr.graph.options);
  CHECK(o.converged);
  double good = 0, bad_s = 0, bad_r = 0;
  for (auto& e : o.final_errors) {
    if (e.edge_id != "bad") good += e.weighted_squared;
    else { bad_s = e.weighted_squared; bad_r = e.robust_cost; }
  }
  CHECK(good < 3.0);          // good edges only slightly displaced
  CHECK(bad_s > 100.0);       // false edge stays hugely inconsistent
  CHECK(bad_r < 0.05 * bad_s);  // robust loss discounts it ~20x+
}

TEST(cancel_does_not_populate_result) {
  // Large problem + pre-solve signal so cancellation lands deterministically
  // regardless of iteration-callback timing.
  json nodes = json::array();
  const int n = 2000;
  for (int i = 0; i < n; ++i)
    nodes.push_back({{"id", "n" + std::to_string(i)},
                     {"init", {1.0 * i + 0.01 * i, 0.0, 0.001 * i}},
                     {"fixed", i == 0}});
  json edges = json::array();
  for (int i = 0; i < n - 1; ++i) {
    edges.push_back({{"id", "e" + std::to_string(i)},
                     {"from", "n" + std::to_string(i)},
                     {"to", "n" + std::to_string(i + 1)},
                     {"measurement", {1.0, 0.0, 0.0}},
                     {"information", {20, 0, 0, 0, 20, 0, 0, 0, 30}}});
    if (i % 25 == 0 && i + 30 < n)
      edges.push_back({{"id", "l" + std::to_string(i)},
                       {"from", "n" + std::to_string(i)},
                       {"to", "n" + std::to_string(i + 30)},
                       {"measurement", {30.0, 0.0, 0.0}},
                       {"information", {10, 0, 0, 0, 10, 0, 0, 0, 10}}});
  }
  json doc = {{"protocol", kInputProtocol},
              {"version", "1.0"},
              {"graph_version", "cancel-unit-v1"},
              {"options",
               {{"anchor_mode", "single"}, {"max_iterations", 1000}}},
              {"nodes", nodes},
              {"edges", edges}};
  auto lr = LoadGraphFromJson(CanonicalJson(doc));
  CHECK(lr.ok);
  Options opts = lr.graph.options;
  opts.cancel_after_ms = 5;
  auto o = Optimize(lr.graph, {"n0"}, opts);
  CHECK(o.cancelled);
  CHECK(o.optimized.empty());
  CHECK(o.final_errors.empty());
  CHECK(!o.initial_errors.empty());
}

// ---- DB ----
TEST(db_roundtrip) {
  char tmpl[] = "/tmp/pgo_test_XXXXXX";
  int fd = mkstemp(tmpl);
  close(fd);
  std::string path = tmpl;
  RunDb db;
  std::string err;
  CHECK(db.Open(path, &err));
  RunRecord r;
  r.run_id = "run-test-1";
  r.created_at = "2026-09-23T00:00:00Z";
  r.graph_version = "g";
  r.input_sha256 = "deadbeef";
  r.status = "solved";
  r.exit_reason = "converged";
  r.anchor_mode = "single";
  r.num_nodes = 2;
  r.num_edges = 1;
  r.num_components = 1;
  r.anchors_json = "[\"n0\"]";
  r.initial_weighted_cost = 2.0;
  r.final_weighted_cost = 0.001;
  r.iterations = 5;
  r.max_iterations = 100;
  r.elapsed_ms = 12.5;
  r.message = "ok";
  r.result_path = "/tmp/out.json";
  r.result_sha256 = "abc";
  r.signed_body = "no";
  r.tool_version = "1.0.0";
  r.ceres_version = "2.2.0";
  CHECK(db.InsertRun(r, &err));
  EdgeError e1{"e0", "n0", "n1", {0.1, 0.2, 0.3}, 0.374, 0.5, 0.2};
  CHECK(db.InsertEdgeErrors("run-test-1", "initial", {e1}, &err));
  RunRecord back;
  CHECK(db.GetRun("run-test-1", &back, &err));
  CHECK(back.graph_version == "g");
  CHECK_NEAR(back.final_weighted_cost, 0.001, 1e-12);
  CHECK(back.status == "solved");
  auto rows = db.ListRuns(10, &err);
  CHECK(rows.size() == 1);
  std::remove(path.c_str());
  std::remove((path + "-wal").c_str());
  std::remove((path + "-shm").c_str());
}

// ---- report signing ----
TEST(report_tamper_and_hmac) {
  auto lr = SquareGraph();
  auto o = Optimize(lr.graph, {"n0"}, lr.graph.options);
  CHECK(o.converged);
  RunRecord r;
  r.run_id = "rr1";
  r.created_at = "2026-09-23T00:00:00Z";
  r.graph_version = lr.graph.graph_version;
  r.input_sha256 = lr.input_sha256;
  r.status = "solved";
  r.exit_reason = "converged";
  r.anchor_mode = "single";
  r.num_nodes = static_cast<int>(lr.graph.nodes.size());
  r.num_edges = static_cast<int>(lr.graph.edges.size());
  r.num_components = 1;
  r.anchors_json = "[\"n0\"]";
  r.initial_weighted_cost = o.initial.weighted_cost;
  r.final_weighted_cost = o.final.weighted_cost;
  r.iterations = o.iterations;
  r.max_iterations = 100;
  r.elapsed_ms = o.elapsed_ms;
  r.message = "ok";
  r.signed_body = "yes";
  r.tool_version = "1.0.0";
  r.ceres_version = "2.2.0";
  std::vector<std::string> anchors = {"n0"};
  ReportInputs in{r, &lr.graph, &anchors, &o, "secret"};
  json doc = BuildReport(in);
  std::string text = CanonicalJson(doc);
  VerifyResult vr = VerifyReport(text, "secret");
  CHECK(vr.ok);
  CHECK(vr.signed_report);
  VerifyResult bad = VerifyReport(text, "nope");
  CHECK(!bad.ok && bad.status == "bad_signature");
  json d2 = json::parse(text);
  d2["body"]["nodes"][0]["final"][0] = 99.0;
  VerifyResult tamp = VerifyReport(CanonicalJson(d2), "secret");
  CHECK(!tamp.ok && tamp.status == "tampered");
}

TEST(missing_graph_version_rejected) {
  auto lr = LoadGraphFromJson(
      R"({"protocol":"pgo-input/1.0","version":"1.0",
          "nodes":[],"edges":[]})");
  CHECK(!lr.ok);
  bool fv = false;
  for (auto& i : lr.validation.issues)
    fv |= i.code == "input.missing_field";
  CHECK(fv);
}

}  // namespace

int main() {
  int failed_tests = 0;
  for (auto& [name, fn] : g_tests) {
    int before = g_failures;
    fn();
    if (g_failures == before) {
      std::printf("[PASS] %s\n", name.c_str());
    } else {
      std::printf("[FAIL] %s (%d checks failed)\n", name.c_str(),
                  g_failures - before);
      ++failed_tests;
    }
  }
  std::printf("\n%d tests, %d checks, %d failed checks, %d failed tests\n",
              static_cast<int>(g_tests.size()), g_checks, g_failures,
              failed_tests);
  return failed_tests == 0 ? 0 : 1;
}
