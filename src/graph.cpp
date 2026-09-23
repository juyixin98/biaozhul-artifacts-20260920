#include "graph.hpp"

#include <Eigen/Cholesky>
#include <Eigen/Core>

#include <cmath>
#include <numeric>
#include <set>

#include "json.hpp"
#include "se2.hpp"

namespace pgo {

namespace {

double requireFiniteNumber(const JsonValue& v, const std::string& what) {
  if (!v.isNumber()) throw ValidationError(what + ": expected a number");
  double d = v.asNumber();
  if (!std::isfinite(d)) throw ValidationError(what + ": value must be finite (got NaN/Infinity)");
  return d;
}

void checkSymmetricPositiveDefinite(const std::array<double, 9>& raw,
                                     Eigen::Matrix3d& sqrtOut,
                                     const std::string& edgeId) {
  Eigen::Matrix3d m;
  for (int r = 0; r < 3; ++r)
    for (int c = 0; c < 3; ++c) m(r, c) = raw[3 * r + c];

  for (int i = 0; i < 9; ++i) {
    if (!std::isfinite(raw[i])) {
      throw ValidationError("edge " + edgeId + ": information matrix contains NaN/Infinity");
    }
  }

  // Symmetry check: off-diagonal pairs must agree (loose relative tolerance;
  // the symmetrized matrix is what the optimizer uses).
  const double scale = std::max(1.0, m.cwiseAbs().maxCoeff());
  for (int r = 0; r < 3; ++r) {
    for (int c = r + 1; c < 3; ++c) {
      if (std::fabs(m(r, c) - m(c, r)) > 1e-9 * scale) {
        throw ValidationError("edge " + edgeId +
                              ": information matrix is not symmetric (M[" +
                              std::to_string(r) + "," + std::to_string(c) + "] vs M[" +
                              std::to_string(c) + "," + std::to_string(r) + "])");
      }
    }
  }

  Eigen::Matrix3d sym = 0.5 * (m + m.transpose());
  Eigen::LLT<Eigen::Matrix3d> llt(sym);
  if (llt.info() != Eigen::Success) {
    throw ValidationError("edge " + edgeId +
                          ": information matrix is not positive definite (Cholesky failed)");
  }
  // Explicit pivot floor guards against matrices LLT accepts but that are
  // effectively singular (ill-conditioned weights fixture relies on the
  // threshold being explicit rather than relying on LLT's internal heuristics).
  // matrixLLT() packs L in its lower triangle, its main diagonal is L's.
  const Eigen::Vector3d pivots = llt.matrixLLT().diagonal().array().square();
  const double pivotFloor = 1e-12 * std::fabs(pivots.maxCoeff());
  if (pivots.minCoeff() <= pivotFloor) {
    throw ValidationError(
        "edge " + edgeId +
        ": information matrix is singular or ill-conditioned (Cholesky pivot <= 1e-12 * max)");
  }
  sqrtOut = llt.matrixL();  // M = L L^T
}

class Dsu {
 public:
  explicit Dsu(int n) : parent_(n), rank_(n, 0) {
    std::iota(parent_.begin(), parent_.end(), 0);
  }
  int find(int x) {
    while (parent_[x] != x) {
      parent_[x] = parent_[parent_[x]];
      x = parent_[x];
    }
    return x;
  }
  void unite(int a, int b) {
    a = find(a);
    b = find(b);
    if (a == b) return;
    if (rank_[a] < rank_[b]) std::swap(a, b);
    parent_[b] = a;
    if (rank_[a] == rank_[b]) ++rank_[a];
  }

 private:
  std::vector<int> parent_;
  std::vector<int> rank_;
};

}  // namespace

Graph loadGraph(const JsonValue& root) {
  if (!root.isObject()) throw ValidationError("top-level JSON value must be an object");

  // The input format version is frozen: anything but 1.0 is rejected outright.
  if (!root.contains("format_version")) {
    throw ValidationError("missing required field: format_version");
  }
  const JsonValue& fv = root.at("format_version");
  if (!fv.isString() || fv.asString() != kFormatVersion) {
    throw ValidationError(std::string("unsupported format_version: expected \"") + kFormatVersion +
                          "\", got \"" + (fv.isString() ? fv.asString() : "<non-string>") + "\"");
  }

  Graph g;
  g.formatVersion = fv.asString();
  if (const JsonValue& name = root.find("graph_name"); name.isString()) {
    g.graphName = name.asString();
  }

  const JsonValue& nodesJson = root.at("nodes");
  if (!nodesJson.isArray() || nodesJson.asArray().empty()) {
    throw ValidationError("\"nodes\" must be a non-empty array");
  }
  std::set<std::string> seenIds;
  g.nodes.reserve(nodesJson.asArray().size());
  for (size_t i = 0; i < nodesJson.asArray().size(); ++i) {
    const JsonValue& nj = nodesJson.asArray()[i];
    if (!nj.isObject()) throw ValidationError("nodes[" + std::to_string(i) + "] must be an object");
    Node n;
    if (!nj.contains("id") || !nj.at("id").isString() || nj.at("id").asString().empty()) {
      throw ValidationError("nodes[" + std::to_string(i) + "]: \"id\" must be a non-empty string");
    }
    n.id = nj.at("id").asString();
    if (!seenIds.insert(n.id).second) {
      throw ValidationError("duplicate node id: " + n.id);
    }
    n.x = requireFiniteNumber(nj.at("x"), "node " + n.id + ".x");
    n.y = requireFiniteNumber(nj.at("y"), "node " + n.id + ".y");
    n.theta = requireFiniteNumber(nj.at("theta"), "node " + n.id + ".theta");
    g.indexById[n.id] = static_cast<int>(g.nodes.size());
    g.nodes.push_back(std::move(n));
  }

  const JsonValue& edgesJson = root.at("edges");
  if (!edgesJson.isArray()) {
    throw ValidationError("\"edges\" must be an array");
  }
  g.edges.reserve(edgesJson.asArray().size());
  std::set<std::string> seenEdgeIds;
  for (size_t i = 0; i < edgesJson.asArray().size(); ++i) {
    const std::string loc = "edges[" + std::to_string(i) + "]";
    const JsonValue& ej = edgesJson.asArray()[i];
    if (!ej.isObject()) throw ValidationError(loc + " must be an object");

    Edge e;
    e.id = "e" + std::to_string(i);
    if (const JsonValue& id = ej.find("id"); id.isString()) {
      if (id.asString().empty()) throw ValidationError(loc + ": edge id must be non-empty");
      e.id = id.asString();
    }
    if (!seenEdgeIds.insert(e.id).second) {
      throw ValidationError("duplicate edge id: " + e.id);
    }

    for (const char* key : {"from", "to"}) {
      if (!ej.contains(key) || !ej.at(key).isString() || ej.at(key).asString().empty()) {
        throw ValidationError("edge " + e.id + ": \"" + key + "\" must be a non-empty string");
      }
    }
    e.from = ej.at("from").asString();
    e.to = ej.at("to").asString();
    if (e.from == e.to) {
      throw ValidationError("edge " + e.id + ": self-loops (from == to) are not supported");
    }
    if (!g.indexById.count(e.from)) {
      throw ValidationError("edge " + e.id + ": unknown node \"" + e.from + "\"");
    }
    if (!g.indexById.count(e.to)) {
      throw ValidationError("edge " + e.id + ": unknown node \"" + e.to + "\"");
    }

    e.dx = requireFiniteNumber(ej.at("dx"), "edge " + e.id + ".dx");
    e.dy = requireFiniteNumber(ej.at("dy"), "edge " + e.id + ".dy");
    e.dtheta = normalizeAngle(
        requireFiniteNumber(ej.at("dtheta"), "edge " + e.id + ".dtheta"));

    const JsonValue& info = ej.at("info");
    if (!info.isArray() || info.asArray().size() != 9) {
      throw ValidationError("edge " + e.id +
                            ": \"info\" must be an array of 9 numbers (row-major 3x3)");
    }
    for (int k = 0; k < 9; ++k) e.info[k] = info.asArray()[k].asNumber();

    Eigen::Matrix3d unused;
    checkSymmetricPositiveDefinite(e.info, unused, e.id);

    g.edges.push_back(std::move(e));
  }

  return g;
}

Components labelComponents(const Graph& g) {
  const int n = static_cast<int>(g.nodes.size());
  Dsu dsu(n);
  for (const Edge& e : g.edges) {
    dsu.unite(g.indexById.at(e.from), g.indexById.at(e.to));
  }

  Components out;
  out.componentOf.assign(n, -1);
  std::map<int, int> rootToComp;
  for (int i = 0; i < n; ++i) {
    int root = dsu.find(i);
    auto mt = rootToComp.find(root);
    int cid;
    if (mt == rootToComp.end()) {
      cid = static_cast<int>(out.members.size());
      rootToComp[root] = cid;
      out.members.emplace_back();
    } else {
      cid = mt->second;
    }
    out.componentOf[i] = cid;
    out.members[cid].push_back(i);
  }
  out.count = static_cast<int>(out.members.size());
  return out;
}

}  // namespace pgo
