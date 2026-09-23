#pragma once
// Load + validate frozen pgo-input/1.0 graphs; connectivity + anchor choice.
#include <string>
#include <vector>

#include "types.h"

namespace pgo {

struct LoadResult {
  bool ok = false;
  Graph graph;
  std::string input_canonical;  // canonical serialization of the input doc
  std::string input_sha256;     // SHA-256 hex of input_canonical (frozen id)
  ValidationReport validation;
};

// Parse from a JSON string. On failure, validation.issues explains why and
// ok is false. A non-SPD information matrix is a fatal error (not a warning).
LoadResult LoadGraphFromJson(const std::string& json_text);

// Read a file and parse it; file errors are reported like parse errors.
LoadResult LoadGraphFromFile(const std::string& path);

// Connected components by undirected edge connectivity (BFS).
std::vector<std::vector<std::string>> ConnectedComponents(const Graph& g);

// Choose anchors given the mode. Returns {} on failure (report explains).
// Single: one component required, anchor = explicit fixed node or lowest id.
// PerComponent: one anchor per component (explicit fixed or lowest id).
std::vector<std::string> SelectAnchors(
    const Graph& g, const std::vector<std::vector<std::string>>& components,
    ValidationReport* report);

}  // namespace pgo
