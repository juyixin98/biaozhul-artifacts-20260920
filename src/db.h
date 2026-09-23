#pragma once
// SQLite-backed run journal. Every invocation is recorded; cancelled runs
// leave a row but never a published result file.
#include <string>
#include <vector>

struct sqlite3;  // forward declaration in the global namespace

#include "types.h"

namespace pgo {

struct RunRecord {
  std::string run_id;
  std::string created_at;
  std::string graph_version;
  std::string input_sha256;
  std::string status;           // solved|failed|cancelled
  std::string exit_reason;
  std::string anchor_mode;
  int num_nodes = 0;
  int num_edges = 0;
  int num_components = 0;
  std::string anchors_json;
  double initial_weighted_cost = 0.0;
  double final_weighted_cost = 0.0;
  int iterations = 0;
  int max_iterations = 0;
  double elapsed_ms = 0.0;
  std::string message;
  std::string result_path;
  std::string result_sha256;
  std::string signed_body;      // "yes"|"no"
  std::string tool_version;
  std::string ceres_version;
};

class RunDb {
 public:
  RunDb() = default;
  ~RunDb();
  RunDb(const RunDb&) = delete;
  RunDb& operator=(const RunDb&) = delete;

  // Opens/creates the DB and applies schema migrations.
  bool Open(const std::string& path, std::string* err);
  bool InsertRun(const RunRecord& rec, std::string* err);
  // Initial errors for every run; final errors only for solved runs.
  bool InsertEdgeErrors(const std::string& run_id, const std::string& phase,
                        const std::vector<EdgeError>& errs, std::string* err);
  bool GetRun(const std::string& run_id, RunRecord* out, std::string* err);
  std::vector<RunRecord> ListRuns(int limit, std::string* err);

 private:
  struct sqlite3* db_ = nullptr;
  bool Exec(const char* sql, std::string* err);
};

}  // namespace pgo
