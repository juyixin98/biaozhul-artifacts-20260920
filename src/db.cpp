#include "db.h"

#include <sqlite3.h>

#include <cstdio>

namespace pgo {

namespace {

const char* kSchema = R"SQL(
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id                 TEXT PRIMARY KEY,
  created_at             TEXT NOT NULL,
  graph_version          TEXT NOT NULL,
  input_sha256           TEXT NOT NULL,
  status                 TEXT NOT NULL,
  exit_reason            TEXT NOT NULL,
  anchor_mode            TEXT NOT NULL,
  num_nodes              INTEGER NOT NULL,
  num_edges              INTEGER NOT NULL,
  num_components         INTEGER NOT NULL,
  anchors                TEXT NOT NULL,
  initial_weighted_cost  REAL NOT NULL,
  final_weighted_cost    REAL NOT NULL,
  iterations             INTEGER NOT NULL,
  max_iterations         INTEGER NOT NULL,
  elapsed_ms             REAL NOT NULL,
  message                TEXT NOT NULL,
  result_path            TEXT NOT NULL,
  result_sha256          TEXT NOT NULL,
  signed_body            TEXT NOT NULL,
  tool_version           TEXT NOT NULL,
  ceres_version          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_errors (
  run_id            TEXT NOT NULL,
  phase             TEXT NOT NULL,   -- initial|final
  edge_id           TEXT NOT NULL,
  from_node         TEXT NOT NULL,
  to_node           TEXT NOT NULL,
  ex                REAL NOT NULL,
  ey                REAL NOT NULL,
  eth               REAL NOT NULL,
  raw_norm          REAL NOT NULL,
  weighted_squared  REAL NOT NULL,
  robust_cost       REAL NOT NULL,
  PRIMARY KEY (run_id, phase, edge_id),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
)SQL";

}  // namespace

bool RunDb::Exec(const char* sql, std::string* err) {
  char* m = nullptr;
  int rc = sqlite3_exec(db_, sql, nullptr, nullptr, &m);
  if (rc != SQLITE_OK) {
    if (err) *err = m ? m : sqlite3_errmsg(db_);
    sqlite3_free(m);
    return false;
  }
  sqlite3_free(m);
  return true;
}

bool RunDb::Open(const std::string& path, std::string* err) {
  if (sqlite3_open(path.c_str(), &db_) != SQLITE_OK) {
    if (err)
      *err = std::string("cannot open database '") + path + "': " +
             sqlite3_errmsg(db_);
    sqlite3_close(db_);
    db_ = nullptr;
    return false;
  }
  sqlite3_busy_timeout(db_, 5000);
  if (!Exec("PRAGMA journal_mode=WAL;", err)) return false;
  if (!Exec("PRAGMA foreign_keys=ON;", err)) return false;
  if (!Exec(kSchema, err)) return false;
  if (!Exec(
          "INSERT OR IGNORE INTO meta(key,value) VALUES "
          "('schema_version','1'),('result_protocol','pgo-result/1.0'),"
          "('input_protocol','pgo-input/1.0');",
          err))
    return false;
  return true;
}

RunDb::~RunDb() {
  if (db_) sqlite3_close(db_);
}

bool RunDb::InsertRun(const RunRecord& rec, std::string* err) {
  const char* sql =
      "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)";
  sqlite3_stmt* st = nullptr;
  if (sqlite3_prepare_v2(db_, sql, -1, &st, nullptr) != SQLITE_OK) {
    if (err) *err = sqlite3_errmsg(db_);
    return false;
  }
  int i = 1;
  auto bind_t = [&](const std::string& s) {
    sqlite3_bind_text(st, i++, s.c_str(), -1, SQLITE_TRANSIENT);
  };
  auto bind_i = [&](long long v) { sqlite3_bind_int64(st, i++, v); };
  auto bind_d = [&](double v) { sqlite3_bind_double(st, i++, v); };
  bind_t(rec.run_id);
  bind_t(rec.created_at);
  bind_t(rec.graph_version);
  bind_t(rec.input_sha256);
  bind_t(rec.status);
  bind_t(rec.exit_reason);
  bind_t(rec.anchor_mode);
  bind_i(rec.num_nodes);
  bind_i(rec.num_edges);
  bind_i(rec.num_components);
  bind_t(rec.anchors_json);
  bind_d(rec.initial_weighted_cost);
  bind_d(rec.final_weighted_cost);
  bind_i(rec.iterations);
  bind_i(rec.max_iterations);
  bind_d(rec.elapsed_ms);
  bind_t(rec.message);
  bind_t(rec.result_path);
  bind_t(rec.result_sha256);
  bind_t(rec.signed_body);
  bind_t(rec.tool_version);
  bind_t(rec.ceres_version);
  int rc = sqlite3_step(st);
  sqlite3_finalize(st);
  if (rc != SQLITE_DONE) {
    if (err) *err = sqlite3_errmsg(db_);
    return false;
  }
  return true;
}

bool RunDb::InsertEdgeErrors(const std::string& run_id,
                             const std::string& phase,
                             const std::vector<EdgeError>& errs,
                             std::string* err) {
  if (!Exec("BEGIN IMMEDIATE;", err)) return false;
  const char* sql =
      "INSERT OR REPLACE INTO edge_errors "
      "(run_id,phase,edge_id,from_node,to_node,ex,ey,eth,raw_norm,"
      "weighted_squared,robust_cost) VALUES (?,?,?,?,?,?,?,?,?,?,?)";
  sqlite3_stmt* st = nullptr;
  if (sqlite3_prepare_v2(db_, sql, -1, &st, nullptr) != SQLITE_OK) {
    if (err) *err = sqlite3_errmsg(db_);
    Exec("ROLLBACK;", nullptr);
    return false;
  }
  bool ok = true;
  for (const auto& e : errs) {
    int i = 1;
    sqlite3_bind_text(st, i++, run_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, i++, phase.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, i++, e.edge_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, i++, e.from.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, i++, e.to.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_double(st, i++, e.raw_error[0]);
    sqlite3_bind_double(st, i++, e.raw_error[1]);
    sqlite3_bind_double(st, i++, e.raw_error[2]);
    sqlite3_bind_double(st, i++, e.raw_norm);
    sqlite3_bind_double(st, i++, e.weighted_squared);
    sqlite3_bind_double(st, i++, e.robust_cost);
    if (sqlite3_step(st) != SQLITE_DONE) {
      if (err) *err = sqlite3_errmsg(db_);
      ok = false;
      break;
    }
    sqlite3_reset(st);
  }
  sqlite3_finalize(st);
  if (!Exec(ok ? "COMMIT;" : "ROLLBACK;", err)) ok = false;
  return ok;
}

namespace {

void FillRecord(sqlite3_stmt* st, RunRecord* r) {
  int i = 0;
  auto ct = [&]() { return reinterpret_cast<const char*>(sqlite3_column_text(st, i++)); };
  r->run_id = ct();
  r->created_at = ct();
  r->graph_version = ct();
  r->input_sha256 = ct();
  r->status = ct();
  r->exit_reason = ct();
  r->anchor_mode = ct();
  r->num_nodes = sqlite3_column_int(st, i++);
  r->num_edges = sqlite3_column_int(st, i++);
  r->num_components = sqlite3_column_int(st, i++);
  r->anchors_json = ct();
  r->initial_weighted_cost = sqlite3_column_double(st, i++);
  r->final_weighted_cost = sqlite3_column_double(st, i++);
  r->iterations = sqlite3_column_int(st, i++);
  r->max_iterations = sqlite3_column_int(st, i++);
  r->elapsed_ms = sqlite3_column_double(st, i++);
  r->message = ct();
  r->result_path = ct();
  r->result_sha256 = ct();
  r->signed_body = ct();
  r->tool_version = ct();
  r->ceres_version = ct();
}

}  // namespace

bool RunDb::GetRun(const std::string& run_id, RunRecord* out,
                   std::string* err) {
  sqlite3_stmt* st = nullptr;
  if (sqlite3_prepare_v2(db_, "SELECT * FROM runs WHERE run_id=?1", -1, &st,
                         nullptr) != SQLITE_OK) {
    if (err) *err = sqlite3_errmsg(db_);
    return false;
  }
  sqlite3_bind_text(st, 1, run_id.c_str(), -1, SQLITE_TRANSIENT);
  int rc = sqlite3_step(st);
  if (rc != SQLITE_ROW) {
    sqlite3_finalize(st);
    if (err) *err = "run not found";
    return false;
  }
  FillRecord(st, out);
  sqlite3_finalize(st);
  return true;
}

std::vector<RunRecord> RunDb::ListRuns(int limit, std::string* err) {
  std::vector<RunRecord> out;
  sqlite3_stmt* st = nullptr;
  char sql[128];
  std::snprintf(sql, sizeof(sql),
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT %d",
                limit > 0 ? limit : 50);
  if (sqlite3_prepare_v2(db_, sql, -1, &st, nullptr) != SQLITE_OK) {
    if (err) *err = sqlite3_errmsg(db_);
    return out;
  }
  while (sqlite3_step(st) == SQLITE_ROW) {
    RunRecord r;
    FillRecord(st, &r);
    out.push_back(std::move(r));
  }
  sqlite3_finalize(st);
  return out;
}

}  // namespace pgo
