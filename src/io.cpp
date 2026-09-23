#include "io.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "json.hpp"
#include "se2.hpp"
#include "sha256.hpp"

namespace pgo {

std::string readFile(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open input file: " + path);
  std::ostringstream ss;
  ss << f.rdbuf();
  if (!f && !ss.eof()) throw std::runtime_error("cannot read input file: " + path);
  return ss.str();
}

std::string canonicalGraphJson(const std::string& rawJson) {
  // Re-serialize through the parser: validates JSON and gives a canonical,
  // key-sorted compact form whose SHA-256 is content-based.
  return parseJson(rawJson).dump(0);
}

double wrapAngleDouble(double a) { return normalizeAngle(a); }

namespace {

JsonValue edgeErrorJson(const EdgeError& e) {
  JsonValue o = JsonValue::makeObject();
  o.set("edge", JsonValue::makeString(e.edgeId));
  o.set("from", JsonValue::makeString(e.from));
  o.set("to", JsonValue::makeString(e.to));
  JsonValue r = JsonValue::makeArray();
  for (double v : e.residual) r.push(JsonValue::makeNumber(v));
  o.set("residual", r);
  o.set("chi2", JsonValue::makeNumber(e.chi2));
  o.set("robust_weight", JsonValue::makeNumber(e.robustWeight));
  return o;
}

}  // namespace

JsonValue buildResultJson(const Graph& graph,
                          const OptimizeResult& result,
                          const std::vector<double>& poses,
                          const OptimizeOptions& options,
                          const std::string& runId,
                          const std::string& createdAt,
                          const std::string& graphHash,
                          const std::string& frozenName,
                          const std::string& inputPath) {
  JsonValue root = JsonValue::makeObject();
  root.set("schema", JsonValue::makeString("pgo-result/1.0"));
  root.set("run_id", JsonValue::makeString(runId));
  root.set("created_at", JsonValue::makeString(createdAt));
  root.set("status", JsonValue::makeString("success"));

  JsonValue in = JsonValue::makeObject();
  in.set("path", JsonValue::makeString(inputPath));
  in.set("format_version", JsonValue::makeString(graph.formatVersion));
  in.set("graph_name", JsonValue::makeString(graph.graphName));
  in.set("graph_sha256", JsonValue::makeString(graphHash));
  in.set("frozen_name", frozenName.empty() ? JsonValue() : JsonValue::makeString(frozenName));
  in.set("num_nodes", JsonValue::makeNumber(static_cast<double>(graph.nodes.size())));
  in.set("num_edges", JsonValue::makeNumber(static_cast<double>(graph.edges.size())));
  root.set("input", in);

  JsonValue cfg = JsonValue::makeObject();
  cfg.set("robust_loss", JsonValue::makeString(robustLossKindName(options.robustLoss)));
  cfg.set("robust_scale", JsonValue::makeNumber(options.robustLossScale));
  cfg.set("max_iterations", JsonValue::makeNumber(options.maxIterations));
  cfg.set("linear_solver",
          JsonValue::makeString(options.useSparseLinearSolver ? "sparse_normal_cholesky"
                                                              : "dense_qr"));
  root.set("config", cfg);

  JsonValue sol = JsonValue::makeObject();
  sol.set("termination", JsonValue::makeString(result.termination));
  sol.set("converged", JsonValue::makeBool(result.converged));
  sol.set("iterations", JsonValue::makeNumber(result.iterations));
  sol.set("message", JsonValue::makeString(result.message));
  root.set("solver", sol);

  JsonValue anch = JsonValue::makeArray();
  for (const std::string& a : result.anchors) anch.push(JsonValue::makeString(a));
  root.set("anchors", anch);

  JsonValue met = JsonValue::makeObject();
  met.set("initial_chi2", JsonValue::makeNumber(result.initialChi2));
  met.set("final_chi2", JsonValue::makeNumber(result.finalChi2));
  met.set("initial_cost", JsonValue::makeNumber(result.initialCost));
  met.set("final_cost", JsonValue::makeNumber(result.finalCost));
  met.set("initial_rms", JsonValue::makeNumber(result.initialRms));
  met.set("final_rms", JsonValue::makeNumber(result.finalRms));
  double ratio = result.initialChi2 > 0.0 ? result.finalChi2 / result.initialChi2 : 0.0;
  met.set("chi2_reduction_ratio", JsonValue::makeNumber(ratio));
  root.set("residuals", met);

  JsonValue nodes = JsonValue::makeArray();
  for (size_t i = 0; i < graph.nodes.size(); ++i) {
    JsonValue n = JsonValue::makeObject();
    n.set("id", JsonValue::makeString(graph.nodes[i].id));
    n.set("x", JsonValue::makeNumber(poses[3 * i]));
    n.set("y", JsonValue::makeNumber(poses[3 * i + 1]));
    n.set("theta", JsonValue::makeNumber(normalizeAngle(poses[3 * i + 2])));
    nodes.push(n);
  }
  root.set("nodes", nodes);

  JsonValue before = JsonValue::makeArray();
  for (const EdgeError& e : result.edgeErrorsBefore) before.push(edgeErrorJson(e));
  JsonValue after = JsonValue::makeArray();
  for (const EdgeError& e : result.edgeErrorsAfter) after.push(edgeErrorJson(e));
  root.set("edge_errors_before", before);
  root.set("edge_errors_after", after);
  return root;
}

void removeIfExists(const std::string& path) {
  struct stat st {};
  if (stat(path.c_str(), &st) == 0) {
    unlink(path.c_str());
  }
}

}  // namespace pgo
