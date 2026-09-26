#pragma once

#include "graph.hpp"
#include "json.hpp"

// Builds a Graph from the request JSON. Throws json::Error / std::invalid_argument
// with a human-readable message on any schema or limit violation.
//
// Accepted request shape:
// {
//   "vertices": ["A", "B", ...] | 5 | omitted (derived from edge endpoints),
//   "edges":    [{"from": 0, "to": 1}, ...]   // int index or string label
// }
Graph buildGraph(const json::Value& request);
