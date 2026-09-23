// pgo_gen — generate acceptance-test pose graphs (format_version 1.0).
//
//   pgo_gen drift-loop   --size N --output out.json   square loop, drifted odom
//   pgo_gen bad-loop     --output out.json             one false loop closure
//   pgo_gen disconnected --output out.json             two components
//   pgo_gen illweight   --output out.json              one singular info matrix
//   pgo_gen large-grid  --grid N --output out.json     N*N grid (cancellation)
//   pgo_gen angle-seam  --output out.json              measurement near +/-pi
//
#include <cmath>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include "graph.hpp"
#include "json.hpp"
#include "se2.hpp"

using namespace pgo;

namespace {

JsonValue makeNode(const std::string& id, double x, double y, double theta) {
  JsonValue n = JsonValue::makeObject();
  n.set("id", JsonValue::makeString(id));
  n.set("x", JsonValue::makeNumber(x));
  n.set("y", JsonValue::makeNumber(y));
  n.set("theta", JsonValue::makeNumber(theta));
  return n;
}

JsonValue makeEdge(const std::string& id, const std::string& from, const std::string& to,
                   double dx, double dy, double dtheta, double sx, double sy, double st) {
  JsonValue e = JsonValue::makeObject();
  e.set("id", JsonValue::makeString(id));
  e.set("from", JsonValue::makeString(from));
  e.set("to", JsonValue::makeString(to));
  e.set("dx", JsonValue::makeNumber(dx));
  e.set("dy", JsonValue::makeNumber(dy));
  e.set("dtheta", JsonValue::makeNumber(normalizeAngle(dtheta)));
  JsonValue info = JsonValue::makeArray();
  double m[9] = {sx, 0, 0, 0, sy, 0, 0, 0, st};
  for (double v : m) info.push(JsonValue::makeNumber(v));
  e.set("info", info);
  return e;
}

JsonValue emptyDoc(const std::string& name) {
  JsonValue root = JsonValue::makeObject();
  root.set("format_version", JsonValue::makeString(kFormatVersion));
  root.set("graph_name", JsonValue::makeString(name));
  root.set("nodes", JsonValue::makeArray());
  root.set("edges", JsonValue::makeArray());
  return root;
}


// A closed square trajectory with drifted initial guesses. Ground truth is
// built by chaining EXACT measurements: four sides of `size` straight
// segments with a pi/2 relative rotation on the four corner edges, plus an
// exact loop-closure edge from the last node back to n0 (identity relative
// pose — the square closes at the origin). Node initial poses integrate the
// same segment lengths but with a deterministic heading jitter added to every
// measured rotation, accumulating pose drift so the closure starts with a
// large residual. All measurements remain consistent with truth, hence the
// optimum chi2 is ~0.
JsonValue driftLoop(int size) {
  JsonValue doc = emptyDoc("drift-loop-" + std::to_string(size));
  JsonValue nodes = JsonValue::makeArray();
  JsonValue edges = JsonValue::makeArray();

  const double side = 10.0;
  const double segLen = side / size;
  const double jitter = 0.012;  // added to each edge rotation ONLY in guess

  auto nid = [](int k) { return "n" + std::to_string(k); };

  // Compose relative pose (d,0,dt) onto (x,y,t) -> next absolute pose.
  auto advance = [](double x, double y, double t, double d, double dt,
                    double& nx, double& ny, double& nt) {
    nx = x + d * std::cos(t);
    ny = y + d * std::sin(t);
    nt = normalizeAngle(t + dt);
  };

  // The 4*size forward edges: last segment of each side carries the pi/2
  // corner rotation.
  const int total = 4 * size;
  std::vector<double> tx(total + 1), ty(total + 1), tt(total + 1);
  tx[0] = ty[0] = tt[0] = 0.0;
  std::vector<double> mDx(total), mDt(total);
  for (int s = 0; s < 4; ++s) {
    for (int k = 0; k < size; ++k) {
      int idx = s * size + k;
      bool corner = (k == size - 1);
      mDx[idx] = segLen;
      mDt[idx] = corner ? kPi / 2.0 : 0.0;
      double nx, ny, nt;
      advance(tx[idx], ty[idx], tt[idx], mDx[idx], mDt[idx], nx, ny, nt);
      tx[idx + 1] = nx; ty[idx + 1] = ny; tt[idx + 1] = nt;
      edges.push(makeEdge("e" + std::to_string(idx), nid(idx), nid(idx + 1),
                          mDx[idx], 0.0, mDt[idx], 200.0, 200.0, 300.0));
    }
  }
  // The final pose is the origin again (theta 2pi == 0). Exact closure.
  edges.push(makeEdge("loop_close", nid(total), nid(0),
                      0.0, 0.0, 0.0, 200.0, 200.0, 300.0));

  // Initial guess: same lengths; jitter every relative rotation.
  double gx = 0.0, gy = 0.0, gt = 0.0;
  for (int k = 0; k <= total; ++k) {
    nodes.push(makeNode(nid(k), gx, gy, gt));
    if (k == total) break;
    double j = ((k % 2) == 0 ? jitter : -0.7 * jitter);
    double nx, ny, nt;
    advance(gx, gy, gt, mDx[k], mDt[k] + j, nx, ny, nt);
    gx = nx; gy = ny; gt = nt;
  }

  doc.set("nodes", nodes);
  doc.set("edges", edges);
  return doc;
}

// Chain of odometry edges whose integration lands node 6 far along +x, plus a
// CORRECT loop closure (n6 -> n0 consistent with the odometry chain) and a
// deliberately WRONG closure on a DIFFERENT, genuinely distant pair
// (n6 -> n3) that claims they coincide. With the good closure and the whole
// odometry chain in agreement, only the bad constraint is an outlier and the
// robust loss must down-weight it alone.
JsonValue badLoop() {
  JsonValue doc = emptyDoc("bad-loop");
  JsonValue nodes = JsonValue::makeArray();
  JsonValue edges = JsonValue::makeArray();

  // Chain n0..n6 in a line, 1 m spacing, with a slightly noisy initial guess.
  for (int k = 0; k <= 6; ++k) {
    nodes.push(makeNode("n" + std::to_string(k), k + 0.03 * k, 0.02 * (k % 2),
                        0.0));
  }
  for (int k = 0; k < 6; ++k) {
    edges.push(makeEdge("odom_" + std::to_string(k), "n" + std::to_string(k),
                        "n" + std::to_string(k + 1), 1.0, 0.0, 0.0, 200.0, 200.0, 300.0));
  }
  // Correct closure: n6 is 6 m ahead of n0 (in n6 frame: -6 along x).
  edges.push(makeEdge("good_close", "n6", "n0", -6.0, 0.0, 0.0, 150.0, 150.0, 200.0));
  // False closure: claims n6 coincides with n3, but they are really 3 m apart.
  // In n6's frame n3 is -3 m along x at truth; claim identity instead.
  edges.push(makeEdge("bad_close", "n6", "n3", 0.0, 0.0, 0.0, 150.0, 150.0, 200.0));

  doc.set("nodes", nodes);
  doc.set("edges", edges);
  return doc;
}

// Two disconnected components: automatic anchoring must pin one node per
// component; single-anchor mode must fail explicitly.
JsonValue disconnected() {
  JsonValue doc = emptyDoc("disconnected");
  JsonValue nodes = JsonValue::makeArray();
  JsonValue edges = JsonValue::makeArray();

  for (int k = 0; k <= 3; ++k)
    nodes.push(makeNode("a" + std::to_string(k), k, 0.0, 0.0));
  for (int k = 0; k < 3; ++k)
    edges.push(makeEdge("ae" + std::to_string(k), "a" + std::to_string(k),
                        "a" + std::to_string(k + 1), 1.0, 0.0, 0.0, 100.0, 100.0, 100.0));

  for (int k = 0; k <= 3; ++k)
    nodes.push(makeNode("b" + std::to_string(k), k, 5.0, kPi / 2.0));
  for (int k = 0; k < 3; ++k)
    edges.push(makeEdge("be" + std::to_string(k), "b" + std::to_string(k),
                        "b" + std::to_string(k + 1), 1.0, 0.0, 0.0, 100.0, 100.0, 100.0));

  doc.set("nodes", nodes);
  doc.set("edges", edges);
  return doc;
}

// One edge with a non-positive-definite information matrix: loader rejects.
JsonValue illWeight() {
  JsonValue doc = emptyDoc("ill-weight");
  JsonValue nodes = JsonValue::makeArray();
  JsonValue edges = JsonValue::makeArray();

  nodes.push(makeNode("n0", 0, 0, 0));
  nodes.push(makeNode("n1", 1, 0, 0));
  nodes.push(makeNode("n2", 2, 0, 0));
  edges.push(makeEdge("ok", "n0", "n1", 1.0, 0.0, 0.0, 100.0, 100.0, 100.0));

  // Zero/negative pivot: theta information is 0 -> semidefinite -> rejected.
  JsonValue e = JsonValue::makeObject();
  e.set("id", JsonValue::makeString("singular"));
  e.set("from", JsonValue::makeString("n1"));
  e.set("to", JsonValue::makeString("n2"));
  e.set("dx", JsonValue::makeNumber(1.0));
  e.set("dy", JsonValue::makeNumber(0.0));
  e.set("dtheta", JsonValue::makeNumber(0.0));
  JsonValue info = JsonValue::makeArray();
  double m[9] = {100, 0, 0, 0, 100, 0, 0, 0, -5};  // negative eigenvalue
  for (double v : m) info.push(JsonValue::makeNumber(v));
  e.set("info", info);
  edges.push(e);

  doc.set("nodes", nodes);
  doc.set("edges", edges);
  return doc;
}

// Dense grid for cancellation tests: many nodes/edges keeps Ceres busy. Edge
// measurements describe an exact unit-spaced grid, but node initial poses are
// perturbed by a deterministic, spatially-varying jitter so the problem starts
// far from the optimum (a perfect guess would solve in zero iterations).
JsonValue largeGrid(int grid) {
  JsonValue doc = emptyDoc("large-grid-" + std::to_string(grid));
  JsonValue nodes = JsonValue::makeArray();
  JsonValue edges = JsonValue::makeArray();

  auto id = [grid](int r, int c) {
    return "g" + std::to_string(r * grid + c);
  };
  auto jitter = [](int k) {
    // Smooth deterministic offset in (-0.12, 0.12), zero on the anchor (0,0).
    return 0.12 * std::sin(0.37 * k + 0.5) * std::cos(0.23 * k);
  };
  for (int r = 0; r < grid; ++r) {
    for (int c = 0; c < grid; ++c) {
      int k = r * grid + c;
      double jx = (c == 0 && r == 0) ? 0.0 : jitter(k);
      double jy = (c == 0 && r == 0) ? 0.0 : jitter(k + 1000);
      double jt = (c == 0 && r == 0) ? 0.0 : 0.05 * std::sin(0.31 * k);
      nodes.push(makeNode(id(r, c), c + jx, r + jy, jt));
    }
  }
  int eid = 0;
  for (int r = 0; r < grid; ++r) {
    for (int c = 0; c < grid; ++c) {
      if (c + 1 < grid)
        edges.push(makeEdge("h" + std::to_string(eid++), id(r, c), id(r, c + 1),
                            1.0, 0.0, 0.0, 50.0, 50.0, 80.0));
      if (r + 1 < grid)
        edges.push(makeEdge("v" + std::to_string(eid++), id(r, c), id(r + 1, c),
                            0.0, 1.0, 0.0, 50.0, 50.0, 80.0));
    }
  }
  doc.set("nodes", nodes);
  doc.set("edges", edges);
  return doc;
}

// Relative pose measurements crossing the +/-pi seam. n0 faces +3.0 rad and
// n1 faces -3.0 rad; the true relative yaw is wrap(-6.0) = +0.283 rad, so a
// naive (unwrapped) subtraction reports a huge error while wrapping gives
// zero. Geometry is fully consistent so the initial chi2 is tiny.
JsonValue angleSeam() {
  JsonValue doc = emptyDoc("angle-seam");
  JsonValue nodes = JsonValue::makeArray();
  JsonValue edges = JsonValue::makeArray();

  const double t0 = 3.0;
  const double t1 = -3.0;
  // n1 sits 1 m directly in front of n0 (which faces t0 = +3.0 rad).
  double x1 = std::cos(t0), y1 = std::sin(t0);
  nodes.push(makeNode("n0", 0.0, 0.0, t0));
  nodes.push(makeNode("n1", x1, y1, t1));
  edges.push(makeEdge("seam", "n0", "n1", 1.0, 0.0,
                      normalizeAngle(t1 - t0), 100.0, 100.0, 200.0));

  // n2: 1 m in front of n1, heading 0.1; raw yaw diff +3.1 wraps to -3.183.
  const double t2 = 0.1;
  double x2 = x1 + std::cos(t1), y2 = y1 + std::sin(t1);
  nodes.push(makeNode("n2", x2, y2, t2));
  edges.push(makeEdge("flip", "n1", "n2", 1.0, 0.0,
                      normalizeAngle(t2 - t1), 100.0, 100.0, 200.0));

  doc.set("nodes", nodes);
  doc.set("edges", edges);
  return doc;
}

void writeDoc(const JsonValue& doc, const std::string& path) {
  std::ofstream f(path, std::ios::binary | std::ios::trunc);
  if (!f) {
    std::cerr << "cannot write " << path << "\n";
    std::exit(1);
  }
  std::string s = doc.dump(2) + "\n";
  f.write(s.data(), static_cast<std::streamsize>(s.size()));
  std::cout << "wrote " << path << "\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: pgo_gen {drift-loop|bad-loop|disconnected|illweight|"
                 "large-grid|angle-seam} [--size N --grid N --output path]\n";
    return 2;
  }
  std::string kind = argv[1];
  int size = 10;
  int grid = 120;
  std::string output;
  for (int i = 2; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--size" && i + 1 < argc) size = std::stoi(argv[++i]);
    else if (a == "--grid" && i + 1 < argc) grid = std::stoi(argv[++i]);
    else if (a == "--output" && i + 1 < argc) output = argv[++i];
    else {
      std::cerr << "unknown/incomplete argument: " << a << "\n";
      return 2;
    }
  }

  JsonValue doc;
  if (kind == "drift-loop") doc = driftLoop(size);
  else if (kind == "bad-loop") doc = badLoop();
  else if (kind == "disconnected") doc = disconnected();
  else if (kind == "illweight") doc = illWeight();
  else if (kind == "large-grid") doc = largeGrid(grid);
  else if (kind == "angle-seam") doc = angleSeam();
  else {
    std::cerr << "unknown kind: " << kind << "\n";
    return 2;
  }

  if (output.empty()) {
    std::cout << doc.dump(2) << "\n";
  } else {
    writeDoc(doc, output);
  }
  return 0;
}
