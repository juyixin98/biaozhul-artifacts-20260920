// Unit tests for the pose graph backend. Tiny hand-rolled framework.
#include <cmath>
#include <cstdio>
#include <functional>
#include <iostream>
#include <string>
#include <vector>

#include "graph.hpp"
#include "io.hpp"
#include "json.hpp"
#include "optimizer.hpp"
#include "se2.hpp"
#include "sha256.hpp"
#include "signal.hpp"

using namespace pgo;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& what) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::cerr << "FAIL: " << what << "\n";
  }
}

bool near(double a, double b, double tol = 1e-8) { return std::fabs(a - b) <= tol; }

// ---------------------------------------------------------------- sha256 ---
void testSha256() {
  // FIPS 180-2 known answers.
  check(sha256Hex("") ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sha256(\"\")");
  check(sha256Hex("abc") ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "sha256(\"abc\")");
  check(sha256Hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
        "sha256(long)");
}

// ---------------------------------------------------------------- se2 ------
void testAngleWrap() {
  check(near(normalizeAngle(0.0), 0.0), "wrap 0");
  check(near(normalizeAngle(3.0), 3.0), "wrap 3");
  check(near(normalizeAngle(-3.0), -3.0), "wrap -3");
  check(near(normalizeAngle(3.0 + 2 * kPi), 3.0, 1e-12), "wrap 3+2pi");
  check(near(normalizeAngle(-3.0 - 2 * kPi), -3.0, 1e-12), "wrap -3-2pi");
  check(near(normalizeAngle(4.0 * kPi), 0.0, 1e-12), "wrap 4pi");
  // Seam: -6.0 wraps to 0.2832..
  check(near(normalizeAngle(-6.0), -6.0 + 2 * kPi, 1e-12), "wrap -6 seam");
  check(near(normalizeAngle(6.0), 6.0 - 2 * kPi, 1e-12), "wrap 6 seam");
}

void testSe2Algebra() {
  // compose with identity.
  double a[3] = {1, 2, 0.5}, id[3] = {0, 0, 0}, out[3];
  se2Compose(a, id, out);
  check(near(out[0], 1) && near(out[1], 2) && near(out[2], 0.5), "compose identity");

  // inverse round trip.
  double inv[3], back[3];
  se2Inverse(a, inv);
  se2Compose(a, inv, back);
  check(near(back[0], 0, 1e-12) && near(back[1], 0, 1e-12) &&
            near(std::fabs(back[2]), 0, 1e-12),
        "compose inverse");

  // Rotation-only inverse: (0,0,pi) inverse is (0,0,-pi) wrapping to pi.
  double rot[3] = {0, 0, kPi}, ri[3];
  se2Inverse(rot, ri);
  check(near(ri[0], 0, 1e-12) && near(ri[1], 0, 1e-12) &&
            near(std::fabs(ri[2]), kPi, 1e-12),
        "inverse of pi rotation");
}

// ---------------------------------------------------------------- json -----
void testJson() {
  JsonValue v = parseJson(R"({"a":[1,2.5,true,null],"b":"x\"y","c":{"d":-3}})");
  check(v.isObject(), "json object");
  check(v.at("a").asArray().size() == 4, "json array size");
  check(near(v.at("a").asArray()[1].asNumber(), 2.5), "json number");
  check(v.at("a").asArray()[2].asBool(), "json bool");
  check(v.at("a").asArray()[3].isNull(), "json null");
  check(v.at("b").asString() == "x\"y", "json string escape");

  bool threw = false;
  try { parseJson("{bad"); } catch (const JsonError& e) { threw = true; check(e.line() == 1, "json error line"); }
  check(threw, "json parse error throws");

  // Round-trip of unicode.
  JsonValue u = parseJson("\"\\u00e9\"");
  check(u.asString() == "\xc3\xa9", "json \\u00e9 -> UTF-8");

  // Canonical serialization ignores whitespace/key order.
  std::string c1 = canonicalGraphJson(R"({ "b": 1, "a": [1, 2] })");
  std::string c2 = canonicalGraphJson(R"({"a":[1,2],"b":1})");
  check(c1 == c2, "canonical json stable");
}

// ---------------------------------------------------------------- graph ----
JsonValue baseDoc() {
  JsonValue root = JsonValue::makeObject();
  root.set("format_version", JsonValue::makeString(kFormatVersion));
  root.set("graph_name", JsonValue::makeString("t"));
  root.set("nodes", JsonValue::makeArray());
  root.set("edges", JsonValue::makeArray());
  return root;
}

void testValidation() {
  // Wrong format version.
  {
    JsonValue d = baseDoc();
    d.set("format_version", JsonValue::makeString("9.9"));
    bool threw = false;
    try { loadGraph(d); } catch (const ValidationError&) { threw = true; }
    check(threw, "reject wrong format_version");
  }
  // Missing node.
  {
    JsonValue d = baseDoc();
    bool threw = false;
    try { loadGraph(d); } catch (const ValidationError&) { threw = true; }
    check(threw, "reject missing nodes");
  }
  // Non-symmetric info.
  {
    JsonValue d = baseDoc();
    JsonValue ns = JsonValue::makeArray();
    ns.push(parseJson(R"({"id":"n0","x":0,"y":0,"theta":0})"));
    ns.push(parseJson(R"({"id":"n1","x":1,"y":0,"theta":0})"));
    d.set("nodes", ns);
    JsonValue es = JsonValue::makeArray();
    JsonValue e = parseJson(R"({"id":"e","from":"n0","to":"n1","dx":1,"dy":0,"dtheta":0})");
    JsonValue info = JsonValue::makeArray();
    double m[9] = {1, 0.5, 0, 0, 1, 0, 0, 0, 1};  // asymmetric
    for (double x : m) info.push(JsonValue::makeNumber(x));
    e.set("info", info);
    es.push(e);
    d.set("edges", es);
    bool threw = false;
    try { loadGraph(d); } catch (const ValidationError&) { threw = true; }
    check(threw, "reject asymmetric info");
  }
  // Negative eigenvalue.
  {
    JsonValue d = baseDoc();
    JsonValue ns = JsonValue::makeArray();
    ns.push(parseJson(R"({"id":"n0","x":0,"y":0,"theta":0})"));
    ns.push(parseJson(R"({"id":"n1","x":1,"y":0,"theta":0})"));
    d.set("nodes", ns);
    JsonValue es = JsonValue::makeArray();
    JsonValue e = parseJson(R"({"id":"e","from":"n0","to":"n1","dx":1,"dy":0,"dtheta":0})");
    JsonValue info = JsonValue::makeArray();
    double m[9] = {1, 0, 0, 0, 1, 0, 0, 0, -2};
    for (double x : m) info.push(JsonValue::makeNumber(x));
    e.set("info", info);
    es.push(e);
    d.set("edges", es);
    bool threw = false;
    try { loadGraph(d); } catch (const ValidationError&) { threw = true; }
    check(threw, "reject non-positive-definite info");
  }
  // Unknown edge endpoint.
  {
    JsonValue d = baseDoc();
    JsonValue ns = JsonValue::makeArray();
    ns.push(parseJson(R"({"id":"n0","x":0,"y":0,"theta":0})"));
    d.set("nodes", ns);
    JsonValue es = JsonValue::makeArray();
    es.push(parseJson(R"({"id":"e","from":"n0","to":"n9","dx":1,"dy":0,"dtheta":0,
                         "info":[1,0,0,0,1,0,0,0,1]})"));
    d.set("edges", es);
    bool threw = false;
    try { loadGraph(d); } catch (const ValidationError&) { threw = true; }
    check(threw, "reject unknown endpoint");
  }
}

JsonValue nodeJson(const std::string& id, double x = 0, double y = 0, double t = 0) {
  JsonValue n = JsonValue::makeObject();
  n.set("id", JsonValue::makeString(id));
  n.set("x", JsonValue::makeNumber(x));
  n.set("y", JsonValue::makeNumber(y));
  n.set("theta", JsonValue::makeNumber(t));
  return n;
}

void testComponents() {
  JsonValue d = baseDoc();
  JsonValue ns = JsonValue::makeArray();
  for (int k = 0; k < 4; ++k)
    ns.push(nodeJson("n" + std::to_string(k)));
  d.set("nodes", ns);
  JsonValue es = JsonValue::makeArray();
  es.push(parseJson(R"({"id":"e0","from":"n0","to":"n1","dx":1,"dy":0,"dtheta":0,
                       "info":[1,0,0,0,1,0,0,0,1]})"));
  es.push(parseJson(R"({"id":"e1","from":"n2","to":"n3","dx":1,"dy":0,"dtheta":0,
                       "info":[1,0,0,0,1,0,0,0,1]})"));
  d.set("edges", es);
  Graph g = loadGraph(d);
  Components c = labelComponents(g);
  check(c.count == 2, "two disconnected components");
  check(c.componentOf[0] == c.componentOf[1], "n0/n1 same component");
  check(c.componentOf[2] == c.componentOf[3], "n2/n3 same component");
  check(c.componentOf[0] != c.componentOf[2], "components differ");
}

// ------------------------------------------------------------- optimize ----
JsonValue makeLinearChain(int n) {
  JsonValue d = baseDoc();
  d.set("graph_name", JsonValue::makeString("chain"));
  JsonValue ns = JsonValue::makeArray();
  for (int k = 0; k < n; ++k)
    ns.push(nodeJson("n" + std::to_string(k), k + 0.05 * k, 0.05, 0.0));
  d.set("nodes", ns);
  JsonValue es = JsonValue::makeArray();
  for (int k = 0; k + 1 < n; ++k) {
    JsonValue e = JsonValue::makeObject();
    e.set("id", JsonValue::makeString("e" + std::to_string(k)));
    e.set("from", JsonValue::makeString("n" + std::to_string(k)));
    e.set("to", JsonValue::makeString("n" + std::to_string(k + 1)));
    e.set("dx", JsonValue::makeNumber(1.0));
    e.set("dy", JsonValue::makeNumber(0.0));
    e.set("dtheta", JsonValue::makeNumber(0.0));
    JsonValue info = JsonValue::makeArray();
    double m[9] = {100, 0, 0, 0, 100, 0, 0, 0, 200};
    for (double x : m) info.push(JsonValue::makeNumber(x));
    e.set("info", info);
    es.push(e);
  }
  d.set("edges", es);
  return d;
}

void testSimpleOptimization() {
  Graph g = loadGraph(makeLinearChain(6));
  std::vector<double> poses;
  OptimizeOptions opt;
  opt.robustLoss = RobustLossKind::Huber;
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  check(!r.cancelled, "chain not cancelled");
  check(r.finalChi2 < r.initialChi2 * 1e-4, "chain chi2 drops 1e4x");
  check(r.anchors.size() == 1, "exactly one anchor for connected graph");
  // Anchor n0 fixed at its (noisy) initial pose; optimized chain straightens.
  check(near(poses[3 * 0], g.nodes[0].x, 1e-10), "anchor x fixed");
  check(near(poses[3 * 1], g.nodes[0].x + 1.0, 1e-4), "n1 at +1m");
  check(near(poses[3 * 5], g.nodes[0].x + 5.0, 1e-4), "n5 at +5m");
  check(r.edgeErrorsAfter.size() == g.edges.size(), "per-edge errors present");
}

void testManualAnchorMissingComponent() {
  // Two components, anchor only one -> explicit failure.
  JsonValue d = baseDoc();
  JsonValue ns = JsonValue::makeArray();
  const char* ids[4] = {"a0", "a1", "b0", "b1"};
  for (const char* id : ids) ns.push(nodeJson(id));
  d.set("nodes", ns);
  JsonValue es = JsonValue::makeArray();
  es.push(parseJson(R"({"id":"e0","from":"a0","to":"a1","dx":1,"dy":0,"dtheta":0,
                       "info":[1,0,0,0,1,0,0,0,1]})"));
  es.push(parseJson(R"({"id":"e1","from":"b0","to":"b1","dx":1,"dy":0,"dtheta":0,
                       "info":[1,0,0,0,1,0,0,0,1]})"));
  d.set("edges", es);
  Graph g = loadGraph(d);

  std::vector<double> poses;
  OptimizeOptions opt;
  opt.fixedAnchors = {"a0"};
  bool threw = false;
  try { optimizePoseGraph(g, poses, opt); } catch (const std::exception& e) {
    threw = true;
    std::string w = e.what();
    check(w.find("no anchor") != std::string::npos, "error names unanchored component");
  }
  check(threw, "unanchored component fails explicitly");

  // Explicitly anchor both -> OK.
  opt.fixedAnchors = {"a0", "b0"};
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  check(!r.cancelled && r.anchors.size() == 2, "manual anchors for both components");
}

void testAutoAnchorDisconnected() {
  JsonValue d = baseDoc();
  JsonValue ns = JsonValue::makeArray();
  for (int k = 0; k < 4; ++k)
    ns.push(nodeJson("n" + std::to_string(k), k, 0.0, 0.0));
  d.set("nodes", ns);
  JsonValue es = JsonValue::makeArray();
  es.push(parseJson(R"({"id":"e0","from":"n0","to":"n1","dx":1,"dy":0,"dtheta":0,
                       "info":[1,0,0,0,1,0,0,0,1]})"));
  es.push(parseJson(R"({"id":"e1","from":"n2","to":"n3","dx":1,"dy":0,"dtheta":0,
                       "info":[1,0,0,0,1,0,0,0,1]})"));
  d.set("edges", es);
  Graph g = loadGraph(d);
  std::vector<double> poses;
  OptimizeOptions opt;
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  check(r.anchors.size() == 2, "auto-anchors both components");
}

void testRobustLossDownweightsOutlier() {
  // Chain n0..n3 (1 m spacing) with a correct closure n3->n0 (-3 m) and a
  // false closure n3->n2 claiming they coincide (really 1 m apart). The
  // odometry chain + good closure agree; only the bad pair is an outlier.
  JsonValue d = baseDoc();
  JsonValue ns = JsonValue::makeArray();
  for (int k = 0; k <= 3; ++k) ns.push(nodeJson("n" + std::to_string(k), k, 0, 0));
  d.set("nodes", ns);
  auto info = [](double s) {
    JsonValue a = JsonValue::makeArray();
    double m[9] = {s, 0, 0, 0, s, 0, 0, 0, s};
    for (double x : m) a.push(JsonValue::makeNumber(x));
    return a;
  };
  auto edge = [&](const char* id, const char* f, const char* t, double dx) {
    JsonValue e = JsonValue::makeObject();
    e.set("id", JsonValue::makeString(id));
    e.set("from", JsonValue::makeString(f));
    e.set("to", JsonValue::makeString(t));
    e.set("dx", JsonValue::makeNumber(dx));
    e.set("dy", JsonValue::makeNumber(0.0));
    e.set("dtheta", JsonValue::makeNumber(0.0));
    e.set("info", info(150.0));
    return e;
  };
  JsonValue es = JsonValue::makeArray();
  es.push(edge("o0", "n0", "n1", 1.0));
  es.push(edge("o1", "n1", "n2", 1.0));
  es.push(edge("o2", "n2", "n3", 1.0));
  es.push(edge("good_close", "n3", "n0", -3.0));
  es.push(edge("bad_close", "n3", "n2", 0.0));  // truth is -1.0
  d.set("edges", es);
  Graph g = loadGraph(d);

  std::vector<double> poses;
  OptimizeOptions opt;
  opt.robustLoss = RobustLossKind::Huber;
  opt.robustLossScale = 1.0;
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  double wGood = 1, wBad = 1;
  for (const auto& e : r.edgeErrorsAfter) {
    if (e.edgeId == "good_close") wGood = e.robustWeight;
    if (e.edgeId == "bad_close") wBad = e.robustWeight;
  }
  check(wBad < 0.3, "bad closure robust weight small");
  check(wGood >= 0.9, "good closure robust weight ~1");
  check(wBad < wGood, "outlier down-weighted relative to inlier");
}

void testAngleSeamResidual() {
  // n0 faces +3.0 rad. The measured relative pose places n1 1 m forward with
  // a small relative yaw dtheta = wrap(-3.0 - 3.0) = 0.283 rad, i.e. n1's
  // absolute heading is -3.0 rad. The raw subtraction -6.0 crosses the +/-pi
  // seam; wrapping makes the whole edge residual vanish at truth.
  JsonValue d = baseDoc();
  double t0 = 3.0, t1 = -3.0;
  double dtheta = normalizeAngle(t1 - t0);
  double n1x = std::cos(t0), n1y = std::sin(t0);
  JsonValue ns = JsonValue::makeArray();
  ns.push(nodeJson("n0", 0.0, 0.0, t0));
  ns.push(nodeJson("n1", n1x, n1y, t1));
  d.set("nodes", ns);
  JsonValue es = JsonValue::makeArray();
  JsonValue e = JsonValue::makeObject();
  e.set("id", JsonValue::makeString("seam"));
  e.set("from", JsonValue::makeString("n0"));
  e.set("to", JsonValue::makeString("n1"));
  e.set("dx", JsonValue::makeNumber(1.0));
  e.set("dy", JsonValue::makeNumber(0.0));
  e.set("dtheta", JsonValue::makeNumber(dtheta));
  JsonValue info = JsonValue::makeArray();
  double m[9] = {100, 0, 0, 0, 100, 0, 0, 0, 200};
  for (double x : m) info.push(JsonValue::makeNumber(x));
  e.set("info", info);
  es.push(e);
  d.set("edges", es);
  Graph g = loadGraph(d);
  std::vector<double> poses;
  OptimizeOptions opt;
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  check(r.initialChi2 < 1e-16, "seam residual near zero at truth (wrapped angle)");
  check(std::fabs(r.edgeErrorsBefore[0].residual[2]) < 1e-12,
        "theta residual wrapped, not -6.0");
}

void testCancellationBeforeStart() {
  Canceller::instance().reset();
  Canceller::instance().requestCancel("test");
  Graph g = loadGraph(makeLinearChain(4));
  std::vector<double> poses;
  OptimizeOptions opt;
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  check(r.cancelled, "cancelled flag propagates");
  check(r.edgeErrorsAfter.empty(), "no post-solve errors on cancellation");
  Canceller::instance().reset();
}

void testIsolatedNodeAutoAnchored() {
  // A node with no incident edges forms a singleton component and must be
  // anchored (a free parameter block Ceres cannot optimize).
  JsonValue d = baseDoc();
  JsonValue ns = JsonValue::makeArray();
  ns.push(nodeJson("a", 0, 0, 0));
  ns.push(nodeJson("b", 1, 0, 0));
  ns.push(nodeJson("z", 5, 5, 1));
  d.set("nodes", ns);
  JsonValue es = JsonValue::makeArray();
  JsonValue e = JsonValue::makeObject();
  e.set("id", JsonValue::makeString("e"));
  e.set("from", JsonValue::makeString("a"));
  e.set("to", JsonValue::makeString("b"));
  e.set("dx", JsonValue::makeNumber(1.0));
  e.set("dy", JsonValue::makeNumber(0.0));
  e.set("dtheta", JsonValue::makeNumber(0.0));
  JsonValue info = JsonValue::makeArray();
  double m[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1};
  for (double x : m) info.push(JsonValue::makeNumber(x));
  e.set("info", info);
  es.push(e);
  d.set("edges", es);

  Graph g = loadGraph(d);
  std::vector<double> poses;
  OptimizeOptions opt;
  OptimizeResult r = optimizePoseGraph(g, poses, opt);
  check(!r.cancelled, "isolated-node graph optimizes");
  check(r.anchors.size() == 2, "both components anchored incl isolated node");
  // The isolated node keeps its exact initial pose.
  int zi = g.indexById.at("z");
  check(near(poses[3 * zi], 5.0) && near(poses[3 * zi + 1], 5.0) &&
            near(poses[3 * zi + 2], 1.0),
        "isolated node fixed at its initial pose");
}

}  // namespace

int main() {
  struct Case { std::string name; std::function<void()> fn; };
  std::vector<Case> cases = {
      {"sha256", testSha256},
      {"angle_wrap", testAngleWrap},
      {"se2_algebra", testSe2Algebra},
      {"json", testJson},
      {"validation", testValidation},
      {"components", testComponents},
      {"simple_optimization", testSimpleOptimization},
      {"manual_anchor_missing", testManualAnchorMissingComponent},
      {"auto_anchor_disconnected", testAutoAnchorDisconnected},
      {"robust_outlier", testRobustLossDownweightsOutlier},
      {"angle_seam", testAngleSeamResidual},
      {"isolated_node", testIsolatedNodeAutoAnchored},
      {"cancellation", testCancellationBeforeStart},
  };
  for (const auto& c : cases) {
    int before = g_failures;
    std::cout << "[ RUN  ] " << c.name << "\n";
    c.fn();
    std::cout << (g_failures > before ? "[ FAIL ] " : "[  OK  ] ") << c.name << "\n";
  }
  std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures ? 1 : 0;
}
