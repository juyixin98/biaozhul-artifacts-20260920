// pgo — 2D SE(2) pose graph optimization backend.
//
//   pgo run    --input graph.json --output result.json [options]
//   pgo freeze --frozen-db pgo.db --name NAME --input graph.json
//   pgo inspect --db pgo.db --run RUN_ID
//
#include <fcntl.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "db.hpp"
#include "graph.hpp"
#include "io.hpp"
#include "json.hpp"
#include "optimizer.hpp"
#include "se2.hpp"
#include "sha256.hpp"
#include "signal.hpp"

namespace {

using namespace pgo;

struct Args {
  std::map<std::string, std::string> str;
  std::vector<std::string> anchors;
  uint64_t cancelAfterMs = 0;
};

void usage() {
  std::cerr <<
      R"(usage:
  pgo run --input GRAPH.json --output RESULT.json [options]
  pgo freeze --frozen-db DB --name NAME --input GRAPH.json
  pgo inspect --db DB --run RUN_ID

run options:
  --db PATH                 SQLite database for run records (default: pgo_runs.db)
  --frozen-db PATH --frozen NAME  require input to match a frozen graph version
  --robust none|huber|cauchy       (default: huber)
  --robust-scale FLOAT             (default: 1.0)
  --max-iterations INT             (default: 100)
  --linear-solver sparse|dense    (default: sparse)
  --threads INT                    (default: 4)
  --anchor NODE_ID                 repeatable; one anchor per component or
                                   omit for automatic per-component anchoring
  --cancel-after-ms N             (test hook: abort solve after N ms)

exit codes: 0 success (incl. max-iterations), 1 solver/system error,
             2 invalid input, 3 frozen mismatch, 130 cancelled
)";
}

std::string shift(int& i, int argc, char** argv) {
  if (i + 1 >= argc) {
    std::cerr << "error: option " << argv[i] << " requires a value\n";
    std::exit(2);
  }
  return argv[++i];
}

std::string generateRunId() {
  // 16 random bytes from /dev/urandom, hashed; truncated hex identifier.
  uint8_t buf[16];
  int fd = open("/dev/urandom", O_RDONLY);
  if (fd >= 0) {
    ssize_t got = 0;
    while (got < 16) {
      ssize_t r = read(fd, buf + got, 16 - got);
      if (r <= 0) break;
      got += r;
    }
    close(fd);
    if (got == 16) {
      return "run_" + sha256Hex(std::string(reinterpret_cast<char*>(buf), 16)).substr(0, 16);
    }
  }
  // Fallback (should not happen on Linux): time + address entropy.
  std::random_device rd;
  std::mt19937_64 gen(rd());
  std::uniform_int_distribution<uint64_t> dist;
  std::string s = std::to_string(dist(gen)) + std::to_string(dist(gen));
  return "run_" + sha256Hex(s).substr(0, 16);
}

int cmdFreeze(const Args& a) {
  std::string dbPath = a.str.count("frozen-db") ? a.str.at("frozen-db") : "pgo_frozen.db";
  std::string name = a.str.count("name") ? a.str.at("name") : "";
  std::string input = a.str.count("input") ? a.str.at("input") : "";
  if (name.empty() || input.empty()) {
    std::cerr << "error: freeze requires --frozen-db/--name/--input\n";
    return 2;
  }
  try {
    std::string raw = readFile(input);
    std::string canonical = canonicalGraphJson(raw);  // validates JSON
    std::string hash = sha256Hex(canonical);
    Database db(dbPath);
    db.freezeGraph(name, raw, hash);
    std::cout << "FROZEN name=" << name << " sha256=" << hash << "\n";
    return 0;
  } catch (const ValidationError& e) {
    std::cerr << "input invalid: " << e.what() << "\n";
    return 2;
  } catch (const std::exception& e) {
    std::cerr << "freeze failed: " << e.what() << "\n";
    return 1;
  }
}

int cmdInspect(const Args& a) {
  std::string dbPath = a.str.count("db") ? a.str.at("db") : "pgo_runs.db";
  std::string runId = a.str.count("run") ? a.str.at("run") : "";
  if (runId.empty()) {
    std::cerr << "error: inspect requires --db and --run\n";
    return 2;
  }
  try {
    // Read-only inspection of the run record.
    sqlite3* raw = nullptr;
    if (sqlite3_open_v2(dbPath.c_str(), &raw, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) {
      std::cerr << "cannot open database " << dbPath << "\n";
      return 1;
    }
    sqlite3_stmt* st = nullptr;
    sqlite3_prepare_v2(raw,
                       "SELECT run_id, created_at, status, num_nodes, num_edges, "
                       "num_components, termination, iterations, initial_chi2, "
                       "final_chi2, graph_sha256, cancel_reason FROM runs WHERE run_id = ?1",
                       -1, &st, nullptr);
    sqlite3_bind_text(st, 1, runId.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(st) != SQLITE_ROW) {
      std::cerr << "no run " << runId << " in " << dbPath << "\n";
      sqlite3_finalize(st);
      sqlite3_close(raw);
      return 1;
    }
    auto col = [&](int i) -> std::string {
      const unsigned char* t = sqlite3_column_text(st, i);
      return t ? reinterpret_cast<const char*>(t) : "";
    };
    std::cout << "run_id=" << col(0) << " created_at=" << col(1) << " status=" << col(2)
              << " nodes=" << col(3) << " edges=" << col(4) << " components=" << col(5)
              << " termination=" << col(6) << " iterations=" << col(7)
              << " initial_chi2=" << col(8) << " final_chi2=" << col(9)
              << " graph_sha256=" << col(10);
    if (!col(11).empty()) std::cout << " cancel_reason=" << col(11);
    std::cout << "\n";
    sqlite3_finalize(st);
    sqlite3_close(raw);
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "inspect failed: " << e.what() << "\n";
    return 1;
  }
}

int cmdRun(Args& a) {
  std::string input = a.str.count("input") ? a.str.at("input") : "";
  std::string output = a.str.count("output") ? a.str.at("output") : "";
  std::string dbPath = a.str.count("db") ? a.str.at("db") : "pgo_runs.db";
  if (input.empty() || output.empty()) {
    std::cerr << "error: run requires --input and --output\n";
    return 2;
  }

  Canceller::instance().installHandlers();
  Canceller::instance().reset();
  if (a.cancelAfterMs > 0) scheduleCancelAfterMs(a.cancelAfterMs);

  std::string runId = generateRunId();
  std::string createdAt = utcTimestamp();

  Graph graph;
  std::string raw;
  std::string hash;
  std::string frozenName;
  try {
    raw = readFile(input);
    // Validate/parse through canonical form before anything touches the DB.
    std::string canonical = canonicalGraphJson(raw);
    hash = sha256Hex(canonical);

    if (a.str.count("frozen")) {
      frozenName = a.str.at("frozen");
      std::string fdb = a.str.count("frozen-db") ? a.str.at("frozen-db") : dbPath;
      Database fdbConn(fdb);
      Database::FrozenGraph fg = fdbConn.getFrozenGraph(frozenName);
      if (fg.sha256 != hash) {
        std::cerr << "FROZEN MISMATCH: input sha256=" << hash
                  << " does not match frozen \"" << frozenName << "\" sha256=" << fg.sha256
                  << "\nrefusing to optimize a non-frozen input version\n";
        return 3;
      }
    }

    graph = loadGraph(parseJson(raw));
  } catch (const ValidationError& e) {
    std::cerr << "input invalid: " << e.what() << "\n";
    return 2;
  } catch (const JsonError& e) {
    std::cerr << "input invalid (JSON " << e.line() << ":" << e.col() << "): " << e.what()
              << "\n";
    return 2;
  } catch (const std::exception& e) {
    std::cerr << "input error: " << e.what() << "\n";
    return 2;
  }

  Components comps = labelComponents(graph);

  OptimizeOptions opt;
  opt.robustLoss = parseRobustLossKind(a.str.count("robust") ? a.str.at("robust") : "huber");
  opt.robustLossScale = a.str.count("robust-scale") ? std::stod(a.str.at("robust-scale")) : 1.0;
  opt.maxIterations = a.str.count("max-iterations") ? std::stoi(a.str.at("max-iterations")) : 100;
  opt.useSparseLinearSolver = !(a.str.count("linear-solver") && a.str.at("linear-solver") == "dense");
  opt.numThreads = a.str.count("threads") ? std::stoi(a.str.at("threads")) : 4;
  opt.fixedAnchors = a.anchors;

  std::vector<double> poses;
  OptimizeResult result;
  try {
    result = optimizePoseGraph(graph, poses, opt);
  } catch (const std::exception& e) {
    std::cerr << "optimization setup failed: " << e.what() << "\n";
    // Record the failed setup (e.g. unanchored component).
    try {
      Database db(dbPath);
      RunRecord rec;
      rec.runId = runId;
      rec.createdAt = createdAt;
      rec.status = "solver_failed";
      rec.graphHash = hash;
      rec.frozenName = frozenName;
      rec.inputPath = input;
      rec.robustLoss = robustLossKindName(opt.robustLoss);
      rec.robustScale = opt.robustLossScale;
      rec.maxIterations = opt.maxIterations;
      rec.numNodes = static_cast<int>(graph.nodes.size());
      rec.numEdges = static_cast<int>(graph.edges.size());
      rec.numComponents = comps.count;
      rec.termination = "SETUP_ERROR";
      rec.iterations = -1;
      rec.cancelReason = e.what();
      for (const Node& n : graph.nodes) rec.nodeIds.push_back(n.id);
      db.insertRun(rec, {}, {});
    } catch (...) {
      // Never let audit logging mask the original failure.
    }
    return 1;
  }

  // --- Cancellation: no output file, no poses published ---
  if (result.cancelled || Canceller::instance().cancelled()) {
    const char* reason = Canceller::instance().reason();
    std::cout << "RUN_CANCELLED run_id=" << runId
              << " reason=" << (reason && *reason ? reason : "SIGINT/SIGTERM") << "\n";
    std::cerr << "cancelled before completion: no result file published, poses discarded\n";
    try {
      Database db(dbPath);
      RunRecord rec;
      rec.runId = runId;
      rec.createdAt = createdAt;
      rec.status = "cancelled";
      rec.graphHash = hash;
      rec.frozenName = frozenName;
      rec.inputPath = input;
      rec.robustLoss = robustLossKindName(opt.robustLoss);
      rec.robustScale = opt.robustLossScale;
      rec.maxIterations = opt.maxIterations;
      rec.numNodes = static_cast<int>(graph.nodes.size());
      rec.numEdges = static_cast<int>(graph.edges.size());
      rec.numComponents = comps.count;
      rec.termination = result.termination;
      rec.iterations = result.iterations;
      rec.initialChi2 = result.initialChi2;
      rec.initialCost = result.initialCost;
      rec.initialRms = result.initialRms;
      rec.cancelReason = reason && *reason ? reason : "SIGINT/SIGTERM";
      rec.anchors = result.anchors;
      for (const Node& n : graph.nodes) rec.nodeIds.push_back(n.id);
      db.insertRun(rec, {}, result.edgeErrorsBefore);
    } catch (const std::exception& e) {
      std::cerr << "warning: could not record cancellation: " << e.what() << "\n";
    }
    removeIfExists(output);
    return 130;
  }

  // --- Success: build output, commit DB record, then atomically publish ---
  JsonValue resultJson = buildResultJson(graph, result, poses, opt, runId, createdAt, hash,
                                         frozenName, input);
  std::string content = resultJson.dump(2) + "\n";

  RunRecord rec;
  rec.runId = runId;
  rec.createdAt = createdAt;
  rec.status = "success";
  rec.graphHash = hash;
  rec.frozenName = frozenName;
  rec.inputPath = input;
  rec.outputPath = output;
  rec.robustLoss = robustLossKindName(opt.robustLoss);
  rec.robustScale = opt.robustLossScale;
  rec.maxIterations = opt.maxIterations;
  rec.numNodes = static_cast<int>(graph.nodes.size());
  rec.numEdges = static_cast<int>(graph.edges.size());
  rec.numComponents = comps.count;
  rec.termination = result.termination;
  rec.iterations = result.iterations;
  rec.initialChi2 = result.initialChi2;
  rec.finalChi2 = result.finalChi2;
  rec.initialCost = result.initialCost;
  rec.finalCost = result.finalCost;
  rec.initialRms = result.initialRms;
  rec.finalRms = result.finalRms;
  rec.anchors = result.anchors;
  for (const Node& n : graph.nodes) rec.nodeIds.push_back(n.id);
  rec.finalPoses = poses;

  {
    Database db(dbPath);
    db.insertRun(rec, result.edgeErrorsAfter, result.edgeErrorsBefore);
  }
  writeResultAtomically(output, content);

  std::cout << "RUN_OK run_id=" << runId << " graph_sha256=" << hash
            << " components=" << comps.count << "\n"
            << "anchors=";
  for (size_t i = 0; i < result.anchors.size(); ++i) {
    std::cout << (i ? "," : "") << result.anchors[i];
  }
  std::cout << "\n"
            << "termination=" << result.termination << " iterations=" << result.iterations << "\n"
            << std::setprecision(12)
            << "initial_chi2=" << result.initialChi2 << " final_chi2=" << result.finalChi2 << "\n"
            << "initial_cost=" << result.initialCost << " final_cost=" << result.finalCost << "\n"
            << "initial_rms=" << result.initialRms << " final_rms=" << result.finalRms << "\n";

  std::cout << "edge_errors_after (edge: chi2, w):\n";
  for (const EdgeError& e : result.edgeErrorsAfter) {
    std::cout << "  " << e.edgeId << " (" << e.from << "->" << e.to << "): chi2=" << e.chi2
              << " robust_weight=" << e.robustWeight
              << " residual=[" << e.residual[0] << ", " << e.residual[1] << ", " << e.residual[2]
              << "]\n";
  }
  std::cout << "output=" << output << " db=" << dbPath << "\n";
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    usage();
    return 2;
  }
  std::string command = argv[1];
  if (command == "-h" || command == "--help" || command == "help") {
    usage();
    return 0;
  }

  Args a;
  for (int i = 2; i < argc; ++i) {
    std::string flag = argv[i];
    auto take = [&]() -> std::string { return shift(i, argc, argv); };
    if (flag == "--input") a.str["input"] = take();
    else if (flag == "--output") a.str["output"] = take();
    else if (flag == "--db") a.str["db"] = take();
    else if (flag == "--frozen-db") a.str["frozen-db"] = take();
    else if (flag == "--frozen") a.str["frozen"] = take();
    else if (flag == "--name") a.str["name"] = take();
    else if (flag == "--run") a.str["run"] = take();
    else if (flag == "--robust") a.str["robust"] = take();
    else if (flag == "--robust-scale") a.str["robust-scale"] = take();
    else if (flag == "--max-iterations") a.str["max-iterations"] = take();
    else if (flag == "--linear-solver") a.str["linear-solver"] = take();
    else if (flag == "--threads") a.str["threads"] = take();
    else if (flag == "--anchor") a.anchors.push_back(take());
    else if (flag == "--cancel-after-ms") a.cancelAfterMs = std::stoull(take());
    else {
      std::cerr << "error: unknown argument: " << flag << "\n";
      usage();
      return 2;
    }
  }

  try {
    if (command == "run") return cmdRun(a);
    if (command == "freeze") return cmdFreeze(a);
    if (command == "inspect") return cmdInspect(a);
    std::cerr << "error: unknown command: " << command << "\n";
    usage();
    return 2;
  } catch (const std::exception& e) {
    std::cerr << "fatal: " << e.what() << "\n";
    return 1;
  }
}
