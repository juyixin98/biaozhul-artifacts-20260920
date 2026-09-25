// JSON request/response layer for the dominator service.
//
// Request schema:
// {
//   "entry": "A",
//   "nodes": ["A", "B", ...],
//   "edges": [["A","B"], {"from":"A","to":"B"}, ...],
//   "queries": [
//     {"type":"dominates","a":"A","b":"B"},   // reflexive allowed
//     {"type":"strictly_dominates","a":"A","b":"B"},
//     {"type":"idom","node":"B"},
//     {"type":"frontier","node":"A"},
//     {"type":"dominators","node":"B"},
//     {"type":"dominated_by","node":"A"},     // subtree of the dom tree
//     {"type":"dom_chain","node":"B"}         // B, idom(B), ..., entry
//   ],
//   "verify_naive": false
// }
//
// All fields except entry/nodes/edges are optional.
#pragma once

#include <string>

#include "json.hpp"

namespace domtree {

// Processes one JSON request string and returns a serialized JSON
// response string. Application-level errors come back as a well-formed
// JSON object with success=false (the caller chooses the exit code).
std::string handle_request(const std::string& request_text);

}  // namespace domtree
