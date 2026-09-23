#pragma once
// pgo-result/1.0 report assembly and verification.
//
// The result file is { "protocol":..., "body": <B>, "manifest": {...} }.
// manifest.body_sha256 = SHA-256(canonical(B)); if signed,
// manifest.hmac_sha256 = HMAC-SHA256(key, body_sha256). Verification
// recomputes both; a tampered body or a wrong key fails.
#include <nlohmann/json.hpp>

#include <string>
#include <vector>

#include "db.h"
#include "optimizer.h"
#include "types.h"

namespace pgo {

struct ReportInputs {
  RunRecord run;
  const Graph* graph = nullptr;
  const std::vector<std::string>* anchors = nullptr;
  const OptimizeOutcome* outcome = nullptr;
  std::string signing_key;  // empty -> unsigned report
};

struct VerifyResult {
  bool ok = false;
  std::string status;        // ok|tampered|bad_signature|malformed
  std::string message;
  std::string body_sha256;
  bool signed_report = false;
};

// Build + sign the complete result document.
nlohmann::json BuildReport(const ReportInputs& in);

// Verify a serialized result file's content hash and (if present/supplied
// key) HMAC. signed_report is set when the document carries an HMAC.
VerifyResult VerifyReport(const std::string& json_text,
                          const std::string& expected_key);

}  // namespace pgo
