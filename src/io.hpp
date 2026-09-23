// Input/output helpers: file reading, canonical hashing, result JSON.
#ifndef PGO_IO_HPP
#define PGO_IO_HPP

#include <string>
#include <vector>

#include "graph.hpp"
#include "optimizer.hpp"

namespace pgo {

// Read a whole file as bytes. Throws std::runtime_error on failure.
std::string readFile(const std::string& path);

// Canonical serialization for content hashing: compact JSON, keys sorted
// (the parser's object representation keeps sorted order). Whitespace and
// key ordering of the input do not affect the hash.
std::string canonicalGraphJson(const std::string& rawJson);

double wrapAngleDouble(double a);

// Build the result document written atomically to --output.
class JsonValue;
JsonValue buildResultJson(const Graph& graph,
                          const OptimizeResult& result,
                          const std::vector<double>& poses,
                          const OptimizeOptions& options,
                          const std::string& runId,
                          const std::string& createdAt,
                          const std::string& graphHash,
                          const std::string& frozenName,
                          const std::string& inputPath);

// Remove a path if it exists (best-effort; used to keep a stale output from
// being mistaken for a cancelled run's result).
void removeIfExists(const std::string& path);

}  // namespace pgo

#endif
