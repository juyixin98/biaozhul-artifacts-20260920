#include "db.hpp"

#include <time.h>
#include <unistd.h>

#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <stdexcept>

#include "json.hpp"

namespace pgo {

namespace {

[[noreturn]] void throwSqlite(sqlite3* db, const std::string& what, int rc = 0) {
  std::string msg = what + ": " + sqlite3_errmsg(db);
  if (rc) msg += " (rc=" + std::to_string(rc) + ")";
  throw std::runtime_error(msg);
}

}  // namespace

Database::Database(const std::string& path) : path_(path) {
  int rc = sqlite3_open(path.c_str(), &db_);
  if (rc != SQLITE_OK) {
    std::string err = db_ ? sqlite3_errmsg(db_) : "out of memory";
    if (db_) sqlite3_close(db_);
    throw std::runtime_error("cannot open database " + path + ": " + err);
  }
  sqlite3_busy_timeout(db_, 5000);
  migrate();
}

Database::~Database() {
  if (db_) sqlite3_close(db_);
}

void Database::exec(const char* sql) {
  char* err = nullptr;
  int rc = sqlite3_exec(db_, sql, nullptr, nullptr, &err);
  if (rc != SQLITE_OK) {
    std::string msg = err ? err : "unknown error";
    sqlite3_free(err);
    throw std::runtime_error(std::string("SQL error: ") + msg);
  }
}

void Database::migrate() {
  exec("PRAGMA journal_mode=WAL;");
  exec("PRAGMA foreign_keys=ON;");
  exec(
      "CREATE TABLE IF NOT EXISTS frozen_graphs ("
      "  name TEXT PRIMARY KEY,"
      "  sha256 TEXT NOT NULL,"
      "  raw_json TEXT NOT NULL,"
      "  created_at TEXT NOT NULL);");

  exec(
      "CREATE TABLE IF NOT EXISTS runs ("
      "  run_id TEXT PRIMARY KEY,"
      "  created_at TEXT NOT NULL,"
      "  status TEXT NOT NULL CHECK (status IN "
      "    ('success','cancelled','solver_failed','input_invalid')),"
      "  graph_sha256 TEXT NOT NULL,"
      "  frozen_name TEXT,"
      "  input_path TEXT NOT NULL,"
      "  output_path TEXT,"
      "  robust_loss TEXT NOT NULL,"
      "  robust_scale REAL NOT NULL,"
      "  max_iterations INTEGER NOT NULL,"
      "  num_nodes INTEGER NOT NULL,"
      "  num_edges INTEGER NOT NULL,"
      "  num_components INTEGER NOT NULL,"
      "  termination TEXT,"
      "  iterations INTEGER,"
      "  initial_chi2 REAL,"
      "  final_chi2 REAL,"
      "  initial_cost REAL,"
      "  final_cost REAL,"
      "  initial_rms REAL,"
      "  final_rms REAL,"
      "  cancel_reason TEXT);");

  exec(
      "CREATE TABLE IF NOT EXISTS run_anchors ("
      "  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,"
      "  node_id TEXT NOT NULL,"
      "  PRIMARY KEY (run_id, node_id));");

  exec(
      "CREATE TABLE IF NOT EXISTS edge_errors ("
      "  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,"
      "  edge_id TEXT NOT NULL,"
      "  node_from TEXT NOT NULL,"
      "  node_to TEXT NOT NULL,"
      "  phase TEXT NOT NULL CHECK (phase IN ('before','after')),"
      "  e_x REAL NOT NULL, e_y REAL NOT NULL, e_theta REAL NOT NULL,"
      "  chi2 REAL NOT NULL, robust_weight REAL NOT NULL,"
      "  PRIMARY KEY (run_id, edge_id, phase));");

  exec(
      "CREATE TABLE IF NOT EXISTS run_nodes ("
      "  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,"
      "  node_id TEXT NOT NULL,"
      "  ord INTEGER NOT NULL,"
      "  x REAL NOT NULL, y REAL NOT NULL, theta REAL NOT NULL,"
      "  PRIMARY KEY (run_id, node_id));");

  exec("CREATE INDEX IF NOT EXISTS idx_edge_errors_run ON edge_errors(run_id);");
}

void Database::freezeGraph(const std::string& name, const std::string& rawJson,
                           const std::string& hash) {
  sqlite3_stmt* sel = nullptr;
  if (sqlite3_prepare_v2(db_, "SELECT sha256 FROM frozen_graphs WHERE name = ?1", -1,
                         &sel, nullptr) != SQLITE_OK) {
    throwSqlite(db_, "prepare frozen lookup");
  }
  sqlite3_bind_text(sel, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  int rc = sqlite3_step(sel);
  if (rc == SQLITE_ROW) {
    std::string existing = reinterpret_cast<const char*>(sqlite3_column_text(sel, 0));
    sqlite3_finalize(sel);
    if (existing != hash) {
      throw std::runtime_error(
          "frozen graph \"" + name +
          "\" already exists with a DIFFERENT hash (existing=" + existing +
          ", new=" + hash + "): input graph version is frozen and cannot be altered");
    }
    return;  // idempotent re-freeze of identical content
  }
  sqlite3_finalize(sel);
  if (rc != SQLITE_DONE) throwSqlite(db_, "lookup frozen graph", rc);

  sqlite3_stmt* ins = nullptr;
  if (sqlite3_prepare_v2(
          db_,
          "INSERT INTO frozen_graphs(name, sha256, raw_json, created_at) "
          "VALUES (?1, ?2, ?3, ?4)",
          -1, &ins, nullptr) != SQLITE_OK) {
    throwSqlite(db_, "prepare frozen insert");
  }
  std::string ts = utcTimestamp();
  sqlite3_bind_text(ins, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(ins, 2, hash.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(ins, 3, rawJson.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(ins, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
  rc = sqlite3_step(ins);
  sqlite3_finalize(ins);
  if (rc != SQLITE_DONE) throwSqlite(db_, "insert frozen graph", rc);
}

Database::FrozenGraph Database::getFrozenGraph(const std::string& name) {
  sqlite3_stmt* sel = nullptr;
  if (sqlite3_prepare_v2(
          db_, "SELECT name, sha256, raw_json, created_at FROM frozen_graphs WHERE name = ?1",
          -1, &sel, nullptr) != SQLITE_OK) {
    throwSqlite(db_, "prepare frozen get");
  }
  sqlite3_bind_text(sel, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  int rc = sqlite3_step(sel);
  if (rc != SQLITE_ROW) {
    sqlite3_finalize(sel);
    throw std::runtime_error("no frozen graph named \"" + name + "\"");
  }
  FrozenGraph fg;
  fg.name = reinterpret_cast<const char*>(sqlite3_column_text(sel, 0));
  fg.sha256 = reinterpret_cast<const char*>(sqlite3_column_text(sel, 1));
  fg.rawJson = reinterpret_cast<const char*>(sqlite3_column_text(sel, 2));
  fg.createdAt = reinterpret_cast<const char*>(sqlite3_column_text(sel, 3));
  sqlite3_finalize(sel);
  return fg;
}

void Database::insertRun(const RunRecord& rec,
                         const std::vector<EdgeError>& edgeErrorsAfter,
                         const std::vector<EdgeError>& edgeErrorsBefore) {
  exec("BEGIN IMMEDIATE");
  try {
    sqlite3_stmt* ins = nullptr;
    if (sqlite3_prepare_v2(
            db_,
            "INSERT INTO runs(run_id, created_at, status, graph_sha256, frozen_name,"
            " input_path, output_path, robust_loss, robust_scale, max_iterations,"
            " num_nodes, num_edges, num_components, termination, iterations,"
            " initial_chi2, final_chi2, initial_cost, final_cost,"
            " initial_rms, final_rms, cancel_reason) VALUES ("
            "?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,"
            "?16,?17,?18,?19,?20,?21,?22)",
            -1, &ins, nullptr) != SQLITE_OK) {
      throwSqlite(db_, "prepare run insert");
    }
    int p = 1;
    auto bindText = [&](const std::string& s) {
      sqlite3_bind_text(ins, p++, s.c_str(), -1, SQLITE_TRANSIENT);
    };
    auto bindD = [&](double d) { sqlite3_bind_double(ins, p++, d); };
    auto bindI = [&](long long i) { sqlite3_bind_int64(ins, p++, i); };
    bindText(rec.runId);
    bindText(rec.createdAt);
    bindText(rec.status);
    bindText(rec.graphHash);
    if (rec.frozenName.empty()) sqlite3_bind_null(ins, p++);
    else bindText(rec.frozenName);
    bindText(rec.inputPath);
    if (rec.outputPath.empty()) sqlite3_bind_null(ins, p++);
    else bindText(rec.outputPath);
    bindText(rec.robustLoss);
    bindD(rec.robustScale);
    bindI(rec.maxIterations);
    bindI(rec.numNodes);
    bindI(rec.numEdges);
    bindI(rec.numComponents);
    if (rec.termination.empty()) sqlite3_bind_null(ins, p++);
    else bindText(rec.termination);
    if (rec.iterations < 0) sqlite3_bind_null(ins, p++);
    else bindI(rec.iterations);
    auto bindOptD = [&](double d) {
      if (!std::isfinite(d) && rec.status != "success") sqlite3_bind_null(ins, p++);
      else bindD(d);
    };
    bindOptD(rec.initialChi2);
    bindOptD(rec.finalChi2);
    bindOptD(rec.initialCost);
    bindOptD(rec.finalCost);
    bindOptD(rec.initialRms);
    bindOptD(rec.finalRms);
    if (rec.cancelReason.empty()) sqlite3_bind_null(ins, p++);
    else bindText(rec.cancelReason);

    int rc = sqlite3_step(ins);
    sqlite3_finalize(ins);
    if (rc != SQLITE_DONE) throwSqlite(db_, "insert run", rc);

    sqlite3_stmt* ains = nullptr;
    if (sqlite3_prepare_v2(db_,
                           "INSERT INTO run_anchors(run_id, node_id) VALUES (?1, ?2)",
                           -1, &ains, nullptr) != SQLITE_OK) {
      throwSqlite(db_, "prepare anchor insert");
    }
    for (const std::string& a : rec.anchors) {
      sqlite3_bind_text(ains, 1, rec.runId.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(ains, 2, a.c_str(), -1, SQLITE_TRANSIENT);
      rc = sqlite3_step(ains);
      if (rc != SQLITE_DONE) throwSqlite(db_, "insert anchor", rc);
      sqlite3_reset(ains);
    }
    sqlite3_finalize(ains);

    auto bindEdge = [&](const std::vector<EdgeError>& errs, const char* phase) {
      sqlite3_stmt* es = nullptr;
      if (sqlite3_prepare_v2(
              db_,
              "INSERT INTO edge_errors(run_id, edge_id, node_from, node_to, phase,"
              " e_x, e_y, e_theta, chi2, robust_weight) VALUES "
              "(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
              -1, &es, nullptr) != SQLITE_OK) {
        throwSqlite(db_, "prepare edge insert");
      }
      for (const EdgeError& e : errs) {
        sqlite3_bind_text(es, 1, rec.runId.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(es, 2, e.edgeId.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(es, 3, e.from.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(es, 4, e.to.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(es, 5, phase, -1, SQLITE_TRANSIENT);
        for (int k = 0; k < 3; ++k) sqlite3_bind_double(es, 6 + k, e.residual[k]);
        sqlite3_bind_double(es, 9, e.chi2);
        sqlite3_bind_double(es, 10, e.robustWeight);
        rc = sqlite3_step(es);
        if (rc != SQLITE_DONE) throwSqlite(db_, "insert edge error", rc);
        sqlite3_reset(es);
      }
      sqlite3_finalize(es);
    };
    bindEdge(edgeErrorsBefore, "before");
    bindEdge(edgeErrorsAfter, "after");

    if (rec.status == "success" && !rec.finalPoses.empty()) {
      if (static_cast<int>(rec.nodeIds.size()) != rec.numNodes ||
          static_cast<int>(rec.finalPoses.size()) != 3 * rec.numNodes) {
        throw std::runtime_error("run record node ids/poses size mismatch");
      }
      sqlite3_stmt* ns = nullptr;
      if (sqlite3_prepare_v2(
              db_,
              "INSERT INTO run_nodes(run_id, node_id, ord, x, y, theta) "
              "VALUES (?1,?2,?3,?4,?5,?6)",
              -1, &ns, nullptr) != SQLITE_OK) {
        throwSqlite(db_, "prepare node insert");
      }
      for (int i = 0; i < rec.numNodes; ++i) {
        sqlite3_bind_text(ns, 1, rec.runId.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(ns, 2, rec.nodeIds[i].c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(ns, 3, i);
        sqlite3_bind_double(ns, 4, rec.finalPoses[3 * i]);
        sqlite3_bind_double(ns, 5, rec.finalPoses[3 * i + 1]);
        sqlite3_bind_double(ns, 6, rec.finalPoses[3 * i + 2]);
        rc = sqlite3_step(ns);
        if (rc != SQLITE_DONE) throwSqlite(db_, "insert run node", rc);
        sqlite3_reset(ns);
      }
      sqlite3_finalize(ns);
    }
  } catch (...) {
    exec("ROLLBACK");
    throw;
  }
  exec("COMMIT");
}

std::string utcTimestamp() {
  time_t now = time(nullptr);
  struct tm tm {};
  gmtime_r(&now, &tm);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
  return buf;
}

void writeResultAtomically(const std::string& path, const std::string& content) {
  // Temp file in the SAME directory so rename(2) is atomic on one filesystem.
  std::string tmp = path + ".tmp-" + std::to_string(static_cast<long long>(getpid()));
  int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0644);
  if (fd < 0) {
    throw std::runtime_error("cannot create temp output " + tmp + ": " + std::strerror(errno));
  }
  size_t written = 0;
  while (written < content.size()) {
    ssize_t w = write(fd, content.data() + written, content.size() - written);
    if (w < 0) {
      if (errno == EINTR) continue;
      close(fd);
      unlink(tmp.c_str());
      throw std::runtime_error("write failed for " + tmp + ": " + std::strerror(errno));
    }
    written += static_cast<size_t>(w);
  }
  if (fsync(fd) != 0) {
    close(fd);
    unlink(tmp.c_str());
    throw std::runtime_error("fsync failed for " + tmp + ": " + std::strerror(errno));
  }
  close(fd);
  if (rename(tmp.c_str(), path.c_str()) != 0) {
    unlink(tmp.c_str());
    throw std::runtime_error("rename to " + path + " failed: " + std::strerror(errno));
  }
}

}  // namespace pgo
