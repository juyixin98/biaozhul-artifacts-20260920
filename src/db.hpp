// SQLite persistence: frozen graph versions and immutable run records.
#ifndef PGO_DB_HPP
#define PGO_DB_HPP

#include <sqlite3.h>

#include <memory>
#include <string>
#include <vector>

#include "graph.hpp"
#include "optimizer.hpp"

namespace pgo {

struct RunRecord {
  std::string runId;
  std::string createdAt;       // UTC ISO-8601
  std::string status;         // success | cancelled | solver_failed | input_invalid
  std::string graphHash;
  std::string frozenName;     // empty if not frozen
  std::string inputPath;
  std::string outputPath;
  std::string robustLoss;
  double robustScale = 1.0;
  int maxIterations = 0;
  int numNodes = 0;
  int numEdges = 0;
  int numComponents = 0;
  std::string termination;
  int iterations = 0;
  double initialChi2 = 0.0;
  double finalChi2 = 0.0;
  double initialCost = 0.0;
  double finalCost = 0.0;
  double initialRms = 0.0;
  double finalRms = 0.0;
  std::string cancelReason;
  std::vector<std::string> anchors;
  std::vector<std::string> nodeIds;  // ordered, size = numNodes
  std::vector<double> finalPoses;    // 3 * numNodes, empty unless success
};

class Database {
 public:
  explicit Database(const std::string& path);
  ~Database();
  Database(const Database&) = delete;
  Database& operator=(const Database&) = delete;

  // Freeze a graph version: store hash + raw JSON. Re-freezing the same name
  // with the same hash is a no-op; a different hash is an error.
  void freezeGraph(const std::string& name, const std::string& rawJson,
                   const std::string& hash);

  struct FrozenGraph {
    std::string name;
    std::string sha256;
    std::string rawJson;
    std::string createdAt;
  };
  FrozenGraph getFrozenGraph(const std::string& name);

  // Insert one complete run record (with per-edge errors and final poses).
  // The whole insert is one transaction; cancelled runs record metadata only.
  void insertRun(const RunRecord& rec,
                 const std::vector<EdgeError>& edgeErrorsAfter,
                 const std::vector<EdgeError>& edgeErrorsBefore);

 private:
  sqlite3* db_ = nullptr;
  std::string path_;
  void migrate();
  void exec(const char* sql);
};

// Writes a result JSON document atomically (temp file in the same directory +
// fsync + rename). On any failure the target path is left untouched.
void writeResultAtomically(const std::string& path, const std::string& content);

// UTC timestamp: YYYY-MM-DDTHH:MM:SSZ
std::string utcTimestamp();

}  // namespace pgo

#endif
