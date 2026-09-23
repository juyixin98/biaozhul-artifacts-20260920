#include "graph.h"

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <map>
#include <queue>
#include <set>

#include "canonical.h"
#include "crypto.h"
#include "util.h"

namespace pgo {

using nlohmann::json;
using Eigen::Matrix3d;
using Eigen::Vector3d;

namespace {

void Error(ValidationReport* r, const std::string& code,
           const std::string& msg) {
  r->ok = false;
  r->issues.push_back({"error", code, msg});
}

void Warning(ValidationReport* r, const std::string& code,
             const std::string& msg) {
  r->issues.push_back({"warning", code, msg});
}

bool HasFatal(const ValidationReport& r) {
  return std::any_of(r.issues.begin(), r.issues.end(),
                     [](const ValidationIssue& i) {
                       return i.severity == "error";
                     });
}

bool FinitePose(const Pose& p) {
  return std::isfinite(p.x) && std::isfinite(p.y) &&
         std::isfinite(p.theta);
}

// Symmetric? Positive definite? Eigen LLT + eigenvalue condition number.
// Symmetry is enforced with a strict tolerance (inputs are JSON doubles).
void CheckInformation(const std::array<double, 9>& vals,
                      const std::string& edge_id, ValidationReport* r) {
  Matrix3d M;
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j) M(i, j) = vals[3 * i + j];

  double asym = 0.0;
  for (int i = 0; i < 3; ++i)
    for (int j = i + 1; j < 3; ++j)
      asym = std::max(asym, std::abs(M(i, j) - M(j, i)));
  const double scale = std::max(1.0, M.cwiseAbs().maxCoeff());
  if (asym > 1e-9 * scale) {
    Error(r, "information.asymmetric",
          "edge '" + edge_id + "': information matrix not symmetric (max "
              "asymmetry " + std::to_string(asym) + ")");
    return;
  }
  // Symmetrize defensively before the definite-ness test.
  Matrix3d S = 0.5 * (M + M.transpose());

  Eigen::SelfAdjointEigenSolver<Matrix3d> es(S);
  if (es.info() != Eigen::Success) {
    Error(r, "information.not_positive_definite",
          "edge '" + edge_id + "': eigen-decomposition failed");
    return;
  }
  const auto& ev = es.eigenvalues();
  const double lmin = ev.minCoeff();
  const double lmax = ev.maxCoeff();
  // Positive-definite test. A relative floor a few ULPs above machine
  // epsilon keeps very stiff-but-valid matrices (condition number up to
  // ~1e15) acceptable while still rejecting any non-positive eigenvalue.
  if (!(lmin > 1e-16 * std::max(1.0, lmax)) || !std::isfinite(lmin)) {
    Error(r, "information.not_positive_definite",
          "edge '" + edge_id + "': information matrix rejected "
              "(smallest eigenvalue " + std::to_string(lmin) + ")");
    return;
  }
  // SPD but numerically nasty: accept with a warning.
  const double cond = lmax / lmin;
  if (cond > 1e12) {
    Warning(r, "information.ill_conditioned",
            "edge '" + edge_id + "': information matrix condition number "
                + std::to_string(cond) + " > 1e12 (accepted, ill-conditioned)");
  }
}

std::vector<std::vector<std::string>> Components(const Graph& g) {
  std::map<std::string, std::vector<std::string>> adj;
  for (const auto& kv : g.nodes) adj[kv.first];
  for (const auto& e : g.edges) {
    adj[e.from].push_back(e.to);
    adj[e.to].push_back(e.from);
  }
  std::set<std::string> seen;
  std::vector<std::vector<std::string>> comps;
  for (const auto& kv : g.nodes) {
    if (seen.count(kv.first)) continue;
    std::vector<std::string> comp;
    std::queue<std::string> q;
    q.push(kv.first);
    seen.insert(kv.first);
    while (!q.empty()) {
      std::string u = q.front();
      q.pop();
      comp.push_back(u);
      for (const auto& v : adj[u])
        if (seen.insert(v).second) q.push(v);
    }
    std::sort(comp.begin(), comp.end());
    comps.push_back(std::move(comp));
  }
  std::sort(comps.begin(), comps.end(),
            [](const std::vector<std::string>& a,
               const std::vector<std::string>& b) { return a[0] < b[0]; });
  return comps;
}

const json* ObjField(const json& j, const char* key,
                     const std::string& ctx, bool required,
                     ValidationReport* r) {
  auto it = j.find(key);
  if (it == j.end()) {
    if (required)
      Error(r, "input.missing_field", ctx + ": missing required field '" +
                                          std::string(key) + "'");
    return nullptr;
  }
  return &*it;
}

}  // namespace

std::vector<std::vector<std::string>> ConnectedComponents(const Graph& g) {
  return Components(g);
}

std::vector<std::string> SelectAnchors(
    const Graph& g, const std::vector<std::vector<std::string>>& components,
    ValidationReport* report) {
  std::vector<std::string> anchors;
  if (g.options.anchor_mode == AnchorMode::Single) {
    if (components.size() != 1) {
      Error(report, "graph.disconnected",
            "anchor_mode=single requires one connected component, found "
                + std::to_string(components.size())
                + ". Use anchor_mode=per_component to anchor each component "
                  "explicitly.");
      return {};
    }
    const auto& comp = components[0];
    std::vector<std::string> fixed;
    for (const auto& id : comp)
      if (g.nodes.at(id).fixed_hint) fixed.push_back(id);
    if (fixed.size() > 1) {
      Error(report, "anchor.ambiguous",
            "multiple fixed nodes in component: " + std::to_string(fixed.size()));
      return {};
    }
    anchors.push_back(fixed.empty() ? comp[0] : fixed[0]);
  } else {
    for (const auto& comp : components) {
      std::vector<std::string> fixed;
      for (const auto& id : comp)
        if (g.nodes.at(id).fixed_hint) fixed.push_back(id);
      if (fixed.size() > 1) {
        Error(report, "anchor.ambiguous",
              "component containing '" + comp[0] +
                  "' has multiple fixed nodes");
        return {};
      }
      anchors.push_back(fixed.empty() ? comp[0] : fixed[0]);
    }
  }
  return anchors;
}

LoadResult LoadGraphFromJson(const std::string& json_text) {
  LoadResult lr;
  ValidationReport& r = lr.validation;

  json doc;
  try {
    doc = json::parse(json_text);
  } catch (const std::exception& e) {
    Error(&r, "input.parse_error", std::string("JSON parse failed: ") + e.what());
    return lr;
  }
  if (!doc.is_object()) {
    Error(&r, "input.schema", "top-level JSON must be an object");
    return lr;
  }

  const json* proto = ObjField(doc, "protocol", "<root>", true, &r);
  if (proto) {
    if (!proto->is_string()) {
      Error(&r, "input.protocol", "'protocol' must be a string");
    } else if (proto->get<std::string>() != kInputProtocol) {
      Error(&r, "input.protocol",
            "unsupported protocol '" + proto->get<std::string>() +
                "': this build only accepts " + kInputProtocol);
    }
  }
  const json* ver = ObjField(doc, "version", "<root>", true, &r);
  if (ver) {
    if (!ver->is_string()) {
      Error(&r, "input.version", "'version' must be a string");
    } else {
      lr.graph.protocol_version = ver->get<std::string>();
      if (lr.graph.protocol_version != "1.0") {
        Error(&r, "input.version",
              "unsupported input protocol version '"
                  + lr.graph.protocol_version + "': only 1.0 is implemented");
      }
    }
  }
  // graph_version freezes the input graph and is mandatory.
  const json* gv = ObjField(doc, "graph_version", "<root>", true, &r);
  if (gv) {
    if (!gv->is_string() || gv->get<std::string>().empty())
      Error(&r, "input.graph_version", "'graph_version' must be a non-empty string");
    else
      lr.graph.graph_version = gv->get<std::string>();
  }
  if (const json* nm = ObjField(doc, "name", "<root>", false, &r)) {
    if (nm->is_string())
      lr.graph.name = nm->get<std::string>();
    else
      Error(&r, "input.schema", "'name' must be a string");
  }

  // options
  if (auto it = doc.find("options"); it != doc.end()) {
    if (!it->is_object()) {
      Error(&r, "input.schema", "'options' must be an object");
    } else {
      const json& o = *it;
      if (auto a = o.find("anchor_mode"); a != o.end()) {
        if (!a->is_string() ||
            (a->get<std::string>() != "single" &&
             a->get<std::string>() != "per_component"))
          Error(&r, "options.anchor_mode",
                "anchor_mode must be 'single' or 'per_component'");
        else
          lr.graph.options.anchor_mode =
              a->get<std::string>() == "per_component"
                  ? AnchorMode::PerComponent
                  : AnchorMode::Single;
      }
      if (auto l = o.find("loss_type"); l != o.end()) {
        if (!l->is_string() ||
            (l->get<std::string>() != "huber" &&
             l->get<std::string>() != "cauchy" &&
             l->get<std::string>() != "none"))
          Error(&r, "options.loss_type",
                "loss_type must be huber|cauchy|none");
        else
          lr.graph.options.global_loss_type = l->get<std::string>();
      }
      if (auto p = o.find("loss_param"); p != o.end()) {
        if (!p->is_number() || !std::isfinite(p->get<double>()) ||
            p->get<double>() <= 0)
          Error(&r, "options.loss_param", "loss_param must be > 0");
        else
          lr.graph.options.global_loss_param = p->get<double>();
      }
      if (auto v = o.find("max_iterations"); v != o.end()) {
        if (!v->is_number_integer() || v->get<int>() < 1)
          Error(&r, "options.max_iterations", "max_iterations must be >= 1");
        else
          lr.graph.options.max_iterations = v->get<int>();
      }
      if (auto v = o.find("num_threads"); v != o.end()) {
        if (!v->is_number_integer() || v->get<int>() < 1)
          Error(&r, "options.num_threads", "num_threads must be >= 1");
        else
          lr.graph.options.num_threads = v->get<int>();
      }
      for (const char* tol :
           {"function_tolerance", "gradient_tolerance", "parameter_tolerance"}) {
        if (auto t = o.find(tol); t != o.end()) {
          if (!t->is_number() || !std::isfinite(t->get<double>()) ||
              t->get<double>() < 0)
            Error(&r, std::string("options.") + tol,
                  std::string(tol) + " must be a finite non-negative number");
          else {
            double x = t->get<double>();
            if (std::string(tol) == "function_tolerance")
              lr.graph.options.function_tolerance = x;
            else if (std::string(tol) == "gradient_tolerance")
              lr.graph.options.gradient_tolerance = x;
            else
              lr.graph.options.parameter_tolerance = x;
          }
        }
      }
      if (auto s = o.find("linear_solver"); s != o.end()) {
        if (!s->is_string() ||
            (s->get<std::string>() != "sparse_cholesky" &&
             s->get<std::string>() != "qr"))
          Error(&r, "options.linear_solver",
                "linear_solver must be sparse_cholesky|qr");
        else
          lr.graph.options.linear_solver = s->get<std::string>();
      }
    }
  }

  // nodes
  const json* nodes = ObjField(doc, "nodes", "<root>", true, &r);
  if (nodes && !nodes->is_array())
    Error(&r, "input.schema", "'nodes' must be an array");
  if (nodes && nodes->is_array()) {
    int idx = 0;
    for (const auto& nd : *nodes) {
      std::string ctx = "nodes[" + std::to_string(idx++) + "]";
      if (!nd.is_object()) {
        Error(&r, "input.schema", ctx + " must be an object");
        continue;
      }
      const json* idj = ObjField(nd, "id", ctx, true, &r);
      if (!idj) continue;
      if (!idj->is_string() || idj->get<std::string>().empty()) {
        Error(&r, "input.node_id", ctx + ": id must be a non-empty string");
        continue;
      }
      std::string id = idj->get<std::string>();
      if (lr.graph.nodes.count(id)) {
        Error(&r, "input.duplicate_node", "duplicate node id '" + id + "'");
        continue;
      }
      Node node;
      node.id = id;
      const json* init = ObjField(nd, "init", ctx, true, &r);
      if (init) {
        if (!init->is_array() || init->size() != 3 ||
            !std::all_of(init->begin(), init->end(),
                         [](const json& x) { return x.is_number(); })) {
          Error(&r, "input.pose",
                ctx + ": init must be [x, y, theta] numbers");
        } else {
          node.init.x = (*init)[0].get<double>();
          node.init.y = (*init)[1].get<double>();
          node.init.theta = (*init)[2].get<double>();
          if (!FinitePose(node.init))
            Error(&r, "input.pose", ctx + ": init contains non-finite values");
          else
            node.init.theta = NormalizeAngle(node.init.theta);
        }
      }
      if (auto f = nd.find("fixed"); f != nd.end()) {
        if (!f->is_boolean())
          Error(&r, "input.fixed", ctx + ": fixed must be boolean");
        else
          node.fixed_hint = f->get<bool>();
      }
      if (!HasFatal(r)) lr.graph.nodes[id] = node;
    }
  }

  // edges
  const json* edges = ObjField(doc, "edges", "<root>", true, &r);
  if (edges && !edges->is_array())
    Error(&r, "input.schema", "'edges' must be an array");
  std::set<std::string> edge_ids;
  if (edges && edges->is_array()) {
    int idx = 0;
    for (const auto& ed : *edges) {
      std::string ctx = "edges[" + std::to_string(idx++) + "]";
      if (!ed.is_object()) {
        Error(&r, "input.schema", ctx + " must be an object");
        continue;
      }
      Edge e;
      bool auto_id = false;
      if (auto i = ed.find("id"); i != ed.end()) {
        if (!i->is_string() || i->get<std::string>().empty()) {
          Error(&r, "input.edge_id", ctx + ": id must be a non-empty string");
          continue;
        }
        e.id = i->get<std::string>();
      } else {
        e.id = "edge-" + std::to_string(idx - 1);
        auto_id = true;
      }
      if (!auto_id && !edge_ids.insert(e.id).second) {
        Error(&r, "input.duplicate_edge", "duplicate edge id '" + e.id + "'");
        continue;
      }
      const json* from = ObjField(ed, "from", ctx, true, &r);
      const json* to = ObjField(ed, "to", ctx, true, &r);
      if (from && (!from->is_string() || !lr.graph.nodes.count(
                                            from->get<std::string>())))
        Error(&r, "edge.endpoint",
              "edge '" + e.id + "': unknown node 'from'");
      if (to && (!to->is_string() ||
                 !lr.graph.nodes.count(to->get<std::string>())))
        Error(&r, "edge.endpoint", "edge '" + e.id + "': unknown node 'to'");
      if (from && from->is_string()) e.from = from->get<std::string>();
      if (to && to->is_string()) e.to = to->get<std::string>();
      if (!e.from.empty() && e.from == e.to)
        Error(&r, "edge.self_loop", "edge '" + e.id + "' is a self loop");

      const json* z = ObjField(ed, "measurement", ctx, true, &r);
      if (z) {
        if (!z->is_array() || z->size() != 3 ||
            !std::all_of(z->begin(), z->end(),
                         [](const json& x) { return x.is_number(); })) {
          Error(&r, "input.measurement",
                ctx + ": measurement must be [x, y, theta] numbers");
        } else {
          e.measurement.x = (*z)[0].get<double>();
          e.measurement.y = (*z)[1].get<double>();
          e.measurement.theta = (*z)[2].get<double>();
          if (!FinitePose(e.measurement))
            Error(&r, "input.measurement",
                  "edge '" + e.id + "': non-finite measurement");
          else
            e.measurement.theta = NormalizeAngle(e.measurement.theta);
        }
      }

      const json* inf = ObjField(ed, "information", ctx, true, &r);
      if (inf) {
        if (!inf->is_array() || inf->size() != 9 ||
            !std::all_of(inf->begin(), inf->end(),
                         [](const json& x) { return x.is_number(); })) {
          Error(&r, "input.information",
                ctx + ": information must be 9 numbers (3x3 row-major)");
        } else {
          bool finite = true;
          for (size_t k = 0; k < 9; ++k) {
            e.information[k] = (*inf)[k].get<double>();
            if (!std::isfinite(e.information[k])) finite = false;
          }
          if (!finite)
            Error(&r, "input.information",
                  "edge '" + e.id + "': non-finite information entries");
          else if (!HasFatal(r))
            CheckInformation(e.information, e.id, &r);
        }
      }
      if (auto l = ed.find("loss_type"); l != ed.end()) {
        if (!l->is_string() ||
            (l->get<std::string>() != "default" &&
             l->get<std::string>() != "none" &&
             l->get<std::string>() != "huber" &&
             l->get<std::string>() != "cauchy"))
          Error(&r, "edge.loss_type",
                "edge '" + e.id +
                    "': loss_type must be default|none|huber|cauchy");
        else
          e.loss_type = l->get<std::string>();
      }
      if (auto p = ed.find("loss_param"); p != ed.end()) {
        if (!p->is_number() || !std::isfinite(p->get<double>()) ||
            p->get<double>() <= 0)
          Error(&r, "edge.loss_param",
                "edge '" + e.id + "': loss_param must be > 0");
        else
          e.loss_param = p->get<double>();
      }
      if (!HasFatal(r)) lr.graph.edges.push_back(e);
    }
  }

  if (lr.graph.nodes.empty() && !HasFatal(r))
    Error(&r, "input.empty_graph", "graph has no nodes");

  // connectivity + anchoring policy
  if (!HasFatal(r)) {
    auto comps = Components(lr.graph);
    lr.validation.components = comps;
    if (lr.graph.options.anchor_mode == AnchorMode::Single &&
        comps.size() > 1) {
      Error(&r, "graph.disconnected",
            "graph has " + std::to_string(comps.size()) +
                " disconnected components but anchor_mode=single; "
                "each component must be anchored explicitly "
                "(anchor_mode=per_component) or the run fails.");
    }
    for (const auto& c : comps) {
      std::set<std::string> in(c.begin(), c.end());
      int degree = 0;
      for (const auto& e : lr.graph.edges)
        if (in.count(e.from) && in.count(e.to)) ++degree;
      if (degree == 0)
        Warning(&r, "graph.isolated_component",
                "component rooted at '" + c[0] +
                    "' has no edges (pose unconstrained except anchor)");
    }
  }

  // frozen identity: canonicalize the *input* and hash the bytes.
  if (!HasFatal(r)) {
    try {
      lr.input_canonical = CanonicalJson(doc);
      lr.input_sha256 = Sha256Hex(lr.input_canonical);
      lr.ok = true;
    } catch (const std::exception& e) {
      Error(&r, "input.canonicalization",
            std::string("cannot canonicalize input: ") + e.what());
    }
  }
  return lr;
}

LoadResult LoadGraphFromFile(const std::string& path) {
  LoadResult lr;
  std::string text, err;
  if (!ReadFile(path, &text, &err)) {
    Error(&lr.validation, "input.file", err);
    return lr;
  }
  return LoadGraphFromJson(text);
}

}  // namespace pgo
