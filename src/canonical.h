#pragma once
// Deterministic canonical JSON serialization used for content hashing
// (frozen-input identity and result manifests). Independent of the parser's
// formatting so hashes are stable across whitespace / key ordering.
#include <string>

#include <nlohmann/json.hpp>

namespace pgo {

// Rules: object keys sorted by Unicode code point; 2-space indentation;
// `,` and `:` each followed by a space; non-finite numbers rejected;
// finite doubles emitted with 17 significant digits; strings minimal-escaped.
std::string CanonicalJson(const nlohmann::json& j);

// Case-sensitive hex comparison constant over length/content (best effort).
bool ConstantTimeEquals(const std::string& a, const std::string& b);

}  // namespace pgo
