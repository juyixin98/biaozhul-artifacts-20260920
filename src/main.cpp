#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "canonical.h"
#include "crypto.h"
#include "db.h"
#include "graph.h"
#include "optimizer.h"
#include "report.h"
#include "util.h"

#ifndef PGO_VERSION
#define PGO_VERSION "0.0.0"
#endif
#ifndef PGO_CERES_VERSION_STR
#define PGO_CERES_VERSION_STR "unknown"
#endif

using json = nlohmann::json;
using namespace pgo;

namespace {

// Exit codes are part of the contract — see README.
constexpr int kOk = 0;
constexpr int kUsage = 1;
constexpr int kInput = 2;
constexpr int kDisconnected = 3;
constexpr int kCancelled = 4;
constexpr int kNoConvergence = 5;
constexpr int kVerify = 6;
constexpr int kIo = 7;

struct Args {
  std::map<std::string, std::string> opt;
  std::set<std::string> flags;
  std::vector<std::string> positional;
};

bool ParseArgs(int argc, char** argv, Args* a, std::string* err) {
  for (int i = 1; i < argc; ++i) {
    std::string t = argv[i];
    if (t.rfind("--", 0) == 0) {
      std::string k = t.substr(2);
      if (k == "help" || k == "version") { a->flags.insert(k); continue; }
      auto eq = k.find('=');
      if (eq != std::string::npos) {
        a->opt[k.substr(0, eq)] = k.substr(eq + 1);
      } else if (i + 1 < argc && argv[i + 1][0] != '-') {
        a->opt[k] = argv[++i];
      } else {
        a->flags.insert(k);  // boolean
      }
    } else {
      a->positional.push_back(t);
    }
  }
  (void)err;
  return true;
}

std::string Get(const Args& a, const std::string& k,
                const std::string& def = "") {
  auto it = a.opt.find(k);
  return it == a.opt.end() ? def : it->second;
}

void PrintIssues(const ValidationReport& r) {
  for (const auto& i : r.issues)
    std::cerr << "  " << i.severity << ": " << i.code << ": " << i.message
              << "\n";
}

std::string Join(const std::vector<std::string>& v) {
  std::string s = "[";
  for (size_t i = 0; i < v.size(); ++i) {
    if (i) s += ",";
    s += "\"" + v[i] + "\"";
  }
  s += "]";
  return s;
}

// Resolve a signing key: literal, file:PATH, or env:VAR.
bool ResolveKey(const std::string& spec, std::string* key, std::string* err) {
  const std::string f = "file:";
  const std::string e = "env:";
  if (spec.rfind(f, 0) == 0) {
    if (!ReadFile(spec.substr(f.size()), key, err)) return false;
    while (!key->empty() && (key->back() == '\n' || key->back() == '\r'))
      key->pop_back();
  } else if (spec.rfind(e, 0) == 0) {
    const char* v = std::getenv(spec.substr(e.size()).c_str());
    if (!v) { *err = "environment variable not set"; return false; }
    *key = v;
  } else {
    *key = spec;
  }
  if (key->empty()) { *err = "signing key is empty"; return false; }
  return true;
}

int CmdRun(const Args& a) {
  std::string input = Get(a, "input");
  std::string result = Get(a, "result");
  std::string dbpath = Get(a, "db", "pgo_runs.db");
  if (input.empty() || result.empty()) {
    std::cerr << "run requires --input and --result\n";
    return kUsage;
  }

  RunDb db;
  std::string derr;
  if (!db.Open(dbpath, &derr)) {
    std::cerr << "database error: " << derr << "\n";
    return kIo;
  }

  LoadResult lr = LoadGraphFromFile(input);
  Graph& g = lr.graph;
  std::string run_id = NewRunId();

  auto base_record = [&](const char* status) {
    RunRecord r;
    r.run_id = run_id;
    r.created_at = UtcTimestamp();
    r.graph_version = g.graph_version;
    r.input_sha256 = lr.input_sha256;
    r.status = status;
    r.anchor_mode = g.options.anchor_mode == AnchorMode::PerComponent
                        ? "per_component"
                        : "single";
    r.num_nodes = static_cast<int>(g.nodes.size());
    r.num_edges = static_cast<int>(g.edges.size());
    r.max_iterations = g.options.max_iterations;
    r.tool_version = PGO_VERSION;
    r.ceres_version = PGO_CERES_VERSION_STR;
    return r;
  };

  if (!lr.ok) {
    PrintIssues(lr.validation);
    bool disconnected = false;
    for (const auto& i : lr.validation.issues) {
      disconnected |= i.code == "graph.disconnected";
    }
    RunRecord r = base_record("failed");
    r.exit_reason = disconnected ? "graph.disconnected" : "input.invalid";
    r.num_components =
        static_cast<int>(lr.validation.components.size());
    r.message = lr.validation.issues.empty()
                    ? "parse failed"
                    : lr.validation.issues.front().message;
    db.InsertRun(r, nullptr);
    return disconnected ? kDisconnected : kInput;
  }
  for (const auto& i : lr.validation.issues)
    std::cerr << "  warning: " << i.code << ": " << i.message << "\n";

  // CLI overrides (kept out of the frozen input hash — they are run controls)
  if (!Get(a, "anchor-mode").empty()) {
    std::string m = Get(a, "anchor-mode");
    if (m == "single") g.options.anchor_mode = AnchorMode::Single;
    if (m == "per_component") g.options.anchor_mode = AnchorMode::PerComponent;
  }
  if (!Get(a, "loss").empty()) g.options.global_loss_type = Get(a, "loss");
  auto atoi_or = [](const std::string& s, int def) {
    try { return std::stoi(s); } catch (...) { return def; }
  };
  auto stod_or = [](const std::string& s, double def) {
    try { return std::stod(s); } catch (...) { return def; }
  };
  if (!Get(a, "loss-param").empty())
    g.options.global_loss_param = stod_or(Get(a, "loss-param"), 1.0);
  if (!Get(a, "max-iterations").empty())
    g.options.max_iterations = atoi_or(Get(a, "max-iterations"), 100);
  g.options.cancel_after_ms = atoi_or(Get(a, "cancel-after-ms"), 0);
  g.options.sleep_before_ms = atoi_or(Get(a, "sleep-before-ms"), 0);

  // Re-evaluate connectivity after possible CLI anchor-mode override.
  auto comps = ConnectedComponents(g);
  ValidationReport ar;
  std::vector<std::string> anchors = SelectAnchors(g, comps, &ar);
  if (anchors.empty()) {
    PrintIssues(ar);
    RunRecord r = base_record("failed");
    r.exit_reason = "graph.disconnected";
    r.num_components = static_cast<int>(comps.size());
    r.message = ar.issues.empty() ? "anchor selection failed"
                                  : ar.issues.front().message;
    db.InsertRun(r, nullptr);
    return kDisconnected;
  }

  InstallSignalHandlers();
  OptimizeOutcome o = Optimize(g, anchors, g.options);

  if (o.cancelled) {
    RunRecord r = base_record("cancelled");
    r.exit_reason = "user_cancelled";
    r.num_components = static_cast<int>(comps.size());
    r.anchors_json = Join(anchors);
    r.initial_weighted_cost = o.initial.weighted_cost;
    r.message = o.message;
    db.InsertRun(r, &derr);
    db.InsertEdgeErrors(run_id, "initial", o.initial_errors, nullptr);
    std::cout << "status=cancelled run_id=" << run_id
              << " initial_weighted_cost=" << o.initial.weighted_cost << "\n"
              << "No result published — half-optimized state is never written.\n";
    return kCancelled;
  }

  if (!o.converged) {
    // Failure is reported honestly; no result file is produced.
    RunRecord r = base_record("failed");
    r.exit_reason = "solver." + o.termination;
    r.num_components = static_cast<int>(comps.size());
    r.anchors_json = Join(anchors);
    r.initial_weighted_cost = o.initial.weighted_cost;
    r.final_weighted_cost = o.final.weighted_cost;
    r.iterations = o.iterations;
    r.elapsed_ms = o.elapsed_ms;
    r.message = o.message;
    db.InsertRun(r, nullptr);
    db.InsertEdgeErrors(run_id, "initial", o.initial_errors, nullptr);
    std::cerr << "solver did not converge: " << o.termination << " — "
              << o.message << "\n";
    return kNoConvergence;
  }

  std::string key;
  const std::string key_spec = Get(a, "signing-key");
  if (!key_spec.empty() && !ResolveKey(key_spec, &key, &derr)) {
    std::cerr << "signing key error: " << derr << "\n";
    return kUsage;
  }

  RunRecord r = base_record("solved");
  r.exit_reason = "converged";
  r.num_components = static_cast<int>(comps.size());
  r.anchors_json = Join(anchors);
  r.initial_weighted_cost = o.initial.weighted_cost;
  r.final_weighted_cost = o.final.weighted_cost;
  r.iterations = o.iterations;
  r.elapsed_ms = o.elapsed_ms;
  r.message = o.message;
  r.signed_body = key.empty() ? "no" : "yes";

  ReportInputs ri;
  ri.run = r;
  ri.graph = &g;
  ri.anchors = &anchors;
  ri.outcome = &o;
  ri.signing_key = key;
  json doc = BuildReport(ri);
  std::string out_text = CanonicalJson(doc);

  std::string werr;
  if (!WriteFileAtomic(result, out_text, &werr)) {
    std::cerr << "failed to publish result: " << werr << "\n";
    return kIo;
  }

  r.result_path = result;
  r.result_sha256 = Sha256Hex(out_text);  // digest of the published bytes
  db.InsertRun(r, nullptr);
  db.InsertEdgeErrors(run_id, "initial", o.initial_errors, nullptr);
  db.InsertEdgeErrors(run_id, "final", o.final_errors, nullptr);

  std::cout << std::setprecision(12);
  std::cout << "status=solved run_id=" << run_id << "\n"
            << "graph_version=" << g.graph_version << "\n"
            << "input_sha256=" << lr.input_sha256 << "\n"
            << "anchors=" << r.anchors_json << " components=" << comps.size()
            << "\n"
            << "initial_weighted_cost=" << o.initial.weighted_cost << "\n"
            << "final_weighted_cost=" << o.final.weighted_cost << "\n"
            << "initial_robust_cost=" << o.initial.robust_cost << "\n"
            << "final_robust_cost=" << o.final.robust_cost << "\n"
            << "iterations=" << o.iterations << " elapsed_ms=" << o.elapsed_ms
            << "\n"
            << "result=" << result << " result_sha256=" << r.result_sha256
            << "\n"
            << "signed=" << (key.empty() ? "no" : "yes") << "\n";
  return kOk;
}

int CmdVerify(const Args& a) {
  std::string path = Get(a, "result");
  if (path.empty()) {
    std::cerr << "verify requires --result\n";
    return kUsage;
  }
  std::string text, err;
  if (!ReadFile(path, &text, &err)) {
    std::cerr << err << "\n";
    return kIo;
  }
  std::string key;
  const std::string key_spec = Get(a, "signing-key");
  if (!key_spec.empty() && !ResolveKey(key_spec, &key, &err)) {
    std::cerr << "signing key error: " << err << "\n";
    return kUsage;
  }
  VerifyResult vr = VerifyReport(text, key);
  std::cout << "verify=" << vr.status
            << " body_sha256=" << vr.body_sha256 << "\n"
            << vr.message << "\n";
  return vr.ok ? kOk : kVerify;
}

int CmdHistory(const Args& a) {
  std::string dbpath = Get(a, "db", "pgo_runs.db");
  RunDb db;
  std::string err;
  if (!db.Open(dbpath, &err)) {
    std::cerr << "database error: " << err << "\n";
    return kIo;
  }
  int limit = 20;
  if (!Get(a, "limit").empty()) {
    try { limit = std::stoi(Get(a, "limit")); } catch (...) {}
  }
  auto rows = db.ListRuns(limit, &err);
  std::cout << std::left << std::setw(26) << "run_id" << std::setw(10)
            << "status" << std::setw(18) << "graph_version" << std::setw(22)
            << "initial_cost" << std::setw(22) << "final_cost"
            << "created_at\n";
  std::cout << std::string(110, '-') << "\n";
  std::cout << std::setprecision(6);
  for (const auto& r : rows) {
    std::cout << std::setw(26) << r.run_id.substr(0, 24) << std::setw(10)
              << r.status << std::setw(18)
              << r.graph_version.substr(0, 16) << std::setw(22)
              << r.initial_weighted_cost << std::setw(22)
              << r.final_weighted_cost << r.created_at << "\n";
  }
  return kOk;
}

// Synthetic graph generator for stress/cancel tests.
int CmdGenerate(const Args& a) {
  int n = 200;
  double noise = 0.05;
  int loops = 5;
  uint64_t seed = 1;
  bool disconnected = a.flags.count("disconnected") > 0;
  bool bad_info = a.flags.count("bad-information") > 0;
  if (!Get(a, "nodes").empty())
    try { n = std::stoi(Get(a, "nodes")); } catch (...) {}
  if (!Get(a, "noise").empty())
    try { noise = std::stod(Get(a, "noise")); } catch (...) {}
  if (!Get(a, "loops").empty())
    try { loops = std::stoi(Get(a, "loops")); } catch (...) {}
  if (!Get(a, "seed").empty())
    try { seed = std::stoull(Get(a, "seed")); } catch (...) {}
  std::string gv = Get(a, "graph-version", "synthetic-1");
  std::string out = Get(a, "out");
  if (out.empty()) { std::cerr << "generate requires --out\n"; return kUsage; }

  std::mt19937_64 rng(seed);
  std::normal_distribution<double> jitter(0.0, noise);
  std::uniform_real_distribution<double> u01(0.0, 1.0);

  json nodes = json::array();
  std::vector<std::pair<double, double>> truth;
  for (int i = 0; i < n; ++i) {
    double x, y, t;
    if (disconnected && i >= n / 2) {
      int j = i - n / 2;
      x = 20.0 + j;
      y = 20.0;
      t = 0.0;
    } else {
      x = i + jitter(rng);
      y = jitter(rng) * 0.5;
      t = jitter(rng) * 0.02;
    }
    truth.emplace_back(i < n / 2 ? i : i - n / 2, 0.0);
    nodes.push_back(json{{"id", "n" + std::to_string(i)},
                         {"init", {x, y, t}},
                         {"fixed", i == 0 || (disconnected && i == n / 2)}});
  }

  json edges = json::array();
  auto add_edge = [&](int u, int v, double mx, double my, double mt,
                      double diag = 20.0) {
    json inf = json::array();
    for (int r = 0; r < 3; ++r)
      for (int c = 0; c < 3; ++c)
        inf.push_back(r == c ? (r == 2 ? 30.0 : diag)
                             : (std::abs(r - c) == 0 ? 0.0 : 0.0));
    edges.push_back(json{{"id", "e" + std::to_string(u) + "_" +
                                    std::to_string(v)},
                         {"from", "n" + std::to_string(u)},
                         {"to", "n" + std::to_string(v)},
                         {"measurement", {mx, my, mt}},
                         {"information", inf}});
  };
  int limit = disconnected ? n / 2 : n;
  for (int i = 0; i + 1 < n; ++i) {
    if (disconnected && i == n / 2 - 1) continue;
    double mx = 1.0 + jitter(rng) * 0.02;
    add_edge(i, i + 1, mx, jitter(rng) * 0.01, jitter(rng) * 0.005);
  }
  (void)limit;
  for (int k = 0; k < loops; ++k) {
    int span = std::min(n, 20 + k * 10);
    for (int u = 0; u + span < n; u += std::max(1, n / (loops + 1))) {
      double mx = span + jitter(rng) * 0.05;
      add_edge(u, u + span, mx, jitter(rng) * 0.02, 0.0, 10.0);
    }
  }
  if (bad_info && !edges.empty())
    edges[0]["information"] = {1, 0, 0, 0, -1, 0, 0, 0, 1};

  json doc = {
      {"protocol", kInputProtocol},
      {"version", "1.0"},
      {"graph_version", gv},
      {"name", "synthetic"},
      {"options",
       {{"anchor_mode", disconnected ? "per_component" : "single"},
        {"loss_type", "huber"},
        {"loss_param", 1.0},
        {"max_iterations", 200}}},
      {"nodes", nodes},
      {"edges", edges},
  };
  std::string werr;
  if (!WriteFileAtomic(out, CanonicalJson(doc), &werr)) {
    std::cerr << werr << "\n";
    return kIo;
  }
  std::cout << "wrote " << n << " nodes, " << edges.size() << " edges to "
            << out << "\n";
  return kOk;
}

void Usage() {
  std::cerr << R"(pgo — 2D SE(2) pose graph optimization backend, v)" PGO_VERSION
            R"( (Ceres " PGO_CERES_VERSION_STR R"()

USAGE
  pgo run      --input GRAPH.json --result OUT.json [--db runs.db]
               [--anchor-mode single|per_component] [--loss huber|cauchy|none]
               [--loss-param 1.0] [--max-iterations 100]
               [--cancel-after-ms 0] [--sleep-before-ms 0]
               [--signing-key KEY|file:PATH|env:VAR]
  pgo verify   --result OUT.json [--signing-key KEY|file:PATH|env:VAR]
  pgo generate --out GRAPH.json [--nodes 200] [--noise 0.05] [--loops 5]
               [--seed 1] [--graph-version synthetic-1]
               [--disconnected] [--bad-information]
  pgo history  [--db runs.db] [--limit 20]

EXIT CODES
  0 ok | 1 usage | 2 invalid input (incl. non-SPD) | 3 disconnected/anchor
  4 cancelled | 5 no convergence | 6 verification failure | 7 IO failure

Cancellation (deadline or SIGTERM/SIGINT) records the run in SQLite but
never writes a result file: half-optimized state is never published.
)";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    Usage();
    return kUsage;
  }
  Args a;
  std::string perr;
  ParseArgs(argc, argv, &a, &perr);
  if (a.flags.count("help")) {
    Usage();
    return kOk;
  }
  if (a.flags.count("version")) {
    std::cout << "pgo " PGO_VERSION " (ceres " PGO_CERES_VERSION_STR
              << ", protocol input=" << kInputProtocol
              << " result=" << kResultProtocol << ")\n";
    return kOk;
  }
  std::string cmd = argv[1];
  if (cmd == "run") return CmdRun(a);
  if (cmd == "verify") return CmdVerify(a);
  if (cmd == "history") return CmdHistory(a);
  if (cmd == "generate") return CmdGenerate(a);
  std::cerr << "unknown command: " << cmd << "\n";
  Usage();
  return kUsage;
}
